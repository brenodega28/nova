#!/usr/bin/env python3
"""Command line entry point: listen for the wake word, answer the question aloud.

Her name, prompt, voice and every phrase she says unprompted live in
:mod:`persona`; nothing here spells any of it out.

python main.py
python main.py --plain           # console log instead of the interface
python main.py --llm-model zero:latest
python main.py --no-llm          # transcribe only, do not answer
"""

from __future__ import annotations

import argparse
import contextlib
import os
import queue
import random
import sys
import threading
import time
from collections.abc import Callable

import audio
import broker
import model
import persona
from hit_log import HitLog
from listener import Listener

STICKY_SECONDS = 120.0




def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Listen for a wake word, then transcribe what follows."
    )
    parser.add_argument(
        "--wake-word", default=persona.WAKE_WORD, help="word to watch for"
    )
    parser.add_argument(
        "--wake-model",
        default="base.en",
        help="small, fast Whisper model used to spot the wake word",
    )
    parser.add_argument(
        "--model",
        default="large",
        help="Whisper model used for the question after the wake word",
    )
    parser.add_argument(
        "--device",
        default="auto",
        choices=("auto", "cpu", "cuda", "mps"),
        help="torch device for both models",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="spoken language, or 'auto' to let Whisper detect it",
    )
    parser.add_argument(
        "--input-device",
        default=None,
        help="microphone name or index (see --list-devices)",
    )
    parser.add_argument(
        "--wake-reply",
        default=persona.WAKE_REPLY,
        help="spoken the instant the wake word lands ('' for silence)",
    )
    parser.add_argument(
        "--greeting",
        default=persona.GREETING,
        help="spoken once the microphone is calibrated ('' for silence)",
    )
    parser.add_argument(
        "--thinking-reply",
        nargs="*",
        default=list(persona.THINKING_REPLIES),
        help="spoken while the model works, picked at random ('' for silence)",
    )
    parser.add_argument(
        "--voice",
        default=persona.VOICE,
        help="Piper voice (see python -m piper.download_voices)",
    )
    parser.add_argument(
        "--no-voice", action="store_true", help="never speak, just print"
    )
    parser.add_argument(
        "--llm-model",
        default=model.DEFAULT_MODEL,
        help="Ollama model that answers the question (see 'ollama list')",
    )
    parser.add_argument(
        "--llm-host",
        default=model.DEFAULT_HOST,
        help="where Ollama is listening",
    )
    parser.add_argument(
        "--system",
        default=persona.SYSTEM,
        help="system prompt handed to the model",
    )
    parser.add_argument(
        "--think",
        action="store_true",
        help="let the model reason before answering (slower, silent while it does)",
    )
    parser.add_argument(
        "--history-turns",
        type=int,
        default=model.DEFAULT_HISTORY_TURNS,
        help="how many past exchanges the model is reminded of",
    )
    parser.add_argument(
        "--no-modules",
        action="store_true",
        help="ignore installed modules and just converse",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="transcribe the question but do not answer it",
    )
    parser.add_argument(
        "--on-wake",
        default=None,
        help="shell command run the moment the wake word lands, e.g. 'say yes'",
    )
    parser.add_argument(
        "--wake-confidence",
        type=float,
        default=0.5,
        help="reject a wake hit whose clip scores above this as non-speech",
    )
    parser.add_argument(
        "--question-timeout",
        type=float,
        default=10.0,
        help="seconds to wait for a question before going back to idle",
    )
    parser.add_argument(
        "--window",
        type=float,
        default=1.5,
        help="seconds of audio the wake scanner looks at",
    )
    parser.add_argument(
        "--window-interval",
        type=float,
        default=0.25,
        help="seconds between wake scans while someone is speaking",
    )
    parser.add_argument(
        "--sensitivity",
        type=float,
        default=3.0,
        help="speech gate, as a multiple of the measured noise floor",
    )
    parser.add_argument(
        "--silence",
        type=float,
        default=0.35,
        help="seconds of quiet that end an utterance",
    )
    parser.add_argument(
        "--min-utterance", type=float, default=0.25, help="ignore shorter blips"
    )
    parser.add_argument(
        "--max-utterance",
        type=float,
        default=15.0,
        help="force a transcription after this many seconds of speech",
    )
    parser.add_argument("--log", default=None, help="append events to this JSONL file")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every wake scan, not just the hits",
    )
    parser.add_argument(
        "--no-bell", action="store_true", help="do not ring the terminal bell on a hit"
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="log to the console instead of the full screen interface",
    )
    parser.add_argument(
        "--list-devices", action="store_true", help="list input devices and exit"
    )
    args = parser.parse_args(argv)

    if args.language == "auto":
        args.language = None
    if args.input_device is not None and args.input_device.isdigit():
        args.input_device = int(args.input_device)
    return args


def resolve_device(requested: str) -> str:
    import torch

    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def answer_aloud(
    brain: model.Model,
    talker,
    log: HitLog,
    registry: broker.Registry | None = None,
    runner: broker.Runner | None = None,
) -> Callable[[str], None]:
    """Answer the question, from a module where one fits and from the model where none does.

    A question is routed to a module on its declared phrases alone — deterministic,
    and cheap enough not to bother the model with. The model's job is the half it
    is actually good at: reading "will it rain in Porto over the next three days"
    and filling in ``location`` and ``days``. It never chooses whether a module
    runs, only how it is called, and the module refuses anything it did not
    declare.

    When a phrase matched and the model still declines to call a tool, it has
    usually asked for something it needs — "which city?" — and that question is
    worth speaking as-is. When the module was only a guess at a follow-up, a
    declined tool means the guess was wrong, and the question goes to conversation
    instead. The model is asked once either way.

    Conversation is unchanged: generation runs on its own thread, so a long answer
    finishes generating while the opening sentences are still being spoken. The
    gate is held shut across the whole answer, so the microphone never hears her
    thinking out loud between sentences.
    """

    def say_once(text: str) -> None:
        hold = talker.speaking() if talker else contextlib.nullcontext()
        with hold:
            log.answer_chunk(text)
            if talker:
                talker.say(text, blocking=True)

    recent: dict = {"module": None, "at": 0.0}

    def route(question: str) -> tuple[broker.Installed | None, bool]:
        """Which module should see this, and how sure are we.

        A follow-up carries none of the words that routed the first question —
        "and in Madrid?" names no weather at all — so when nothing matches and a
        module answered a moment ago, that module gets offered the question. It is
        a guess, and the second return value says so.
        """
        if registry is None:
            return None, False
        matched = registry.match(question)
        if matched is not None:
            return matched, False
        last = recent["module"]
        if last is not None and time.time() - recent["at"] <= STICKY_SECONDS:
            return last, True
        return None, False

    def from_module(question: str, started: float) -> bool:
        if registry is None or runner is None:
            return False
        module, guessed = route(question)
        if module is None:
            return False
        tools = runner.schemas(module)
        if not tools:
            return False

        decision = brain.decide(question, tools)
        if decision.tool:
            tool = decision.tool.rpartition(".")[2]
            answer = runner.call(module, tool, decision.args)
            log.used(module.name, tool, answer.seconds)
            spoken = answer.speech or f"{module.name} had nothing to say."
            recent.update(module=module, at=time.time())
        elif guessed:
            return False
        elif decision.speech:
            spoken = decision.speech
        else:
            return False

        say_once(spoken)
        log.answer(spoken, time.time() - started)
        brain.remember(question, spoken)
        return True

    def converse(question: str, started: float) -> None:
        sentences: queue.Queue = queue.Queue()

        def generate() -> None:
            try:
                for sentence in brain.stream(question):
                    sentences.put(sentence)
            except Exception as exc:
                sentences.put(exc)
            finally:
                sentences.put(None)

        said: list[str] = []
        failure: Exception | None = None
        hold = talker.speaking() if talker else contextlib.nullcontext()
        with hold:
            threading.Thread(target=generate, daemon=True).start()
            while True:
                item = sentences.get()
                if item is None:
                    break
                if isinstance(item, Exception):
                    failure = item
                    break
                said.append(item)
                log.answer_chunk(item)
                if talker:
                    talker.say(item, blocking=True)

        log.answer(" ".join(said), time.time() - started)
        if failure is not None:
            raise failure

    def handle(question: str) -> None:
        log.thinking()
        started = time.time()
        try:
            if from_module(question, started):
                return
        except (model.ModelError, broker.Broken) as exc:
            log.error(f"module route failed: {exc}")
        converse(question, started)

    return handle


def lend_tqdm_a_plain_lock() -> None:
    """Stop Whisper's progress bar from needing a subprocess to exist.

    tqdm builds a multiprocessing lock the first time any bar is constructed,
    and registering it spawns a resource tracker that inherits stderr. Under the
    interface that descriptor is no longer the terminal's, the spawn fails, and
    every transcription dies with "bad value(s) in fds_to_keep". A plain thread
    lock is all a single-process app ever needed.
    """
    import tqdm

    tqdm.tqdm.set_lock(threading.RLock())


def assemble(args, log: HitLog, progress) -> Listener:
    """Load everything and wire it together, reporting progress as it goes.

    Called on the main thread in plain mode and from the interface's worker
    thread otherwise, which is why every word of progress goes through ``log``
    rather than straight to stdout.
    """
    device = resolve_device(args.device)
    if device == "mps":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    talker = None
    speak = None
    acknowledge = None
    greeting = None
    muted_until = None
    if not args.no_voice:
        import talk

        talker = talk.configure(voice=args.voice)
        progress(f"voice '{args.voice}'")
        talker.load()
        muted_until = talker.muted_until
        if args.greeting:
            greeting = lambda: talker.say(args.greeting, blocking=False)
        if args.wake_reply:
            speak = lambda: talker.say(args.wake_reply, blocking=False)
        if args.thinking_reply and not args.no_llm:
            acknowledge = lambda: talker.say(
                random.choice(args.thinking_reply), blocking=False
            )

    brain = None
    if not args.no_llm:
        brain = model.Model(
            name=args.llm_model,
            host=args.llm_host,
            system=args.system,
            think=args.think,
            history_turns=args.history_turns,
        )
        progress(f"llm '{args.llm_model}' at {args.llm_host}")
        brain.load()

    registry = runner = None
    if not args.no_modules and not args.no_llm:
        registry = broker.Registry()
        runner = broker.Runner(python=sys.executable)
        for complaint in registry.broken:
            log.error(f"module skipped — {complaint}")
        if registry.modules:
            progress(f"modules {', '.join(sorted(registry.modules))}")

    lend_tqdm_a_plain_lock()
    import whisper

    progress(f"wake model '{args.wake_model}' on {device}")
    wake_model = whisper.load_model(args.wake_model, device=device)
    progress(f"question model '{args.model}' on {device}")
    question_model = whisper.load_model(args.model, device=device)

    return Listener(
        wake_model,
        question_model,
        capture=audio.AudioCapture(
            sensitivity=args.sensitivity,
            silence=args.silence,
            min_utterance=args.min_utterance,
            max_utterance=args.max_utterance,
            window=args.window,
            window_interval=args.window_interval,
            input_device=args.input_device,
        ),
        log=log,
        wake_word=args.wake_word,
        wake_variants=(
            persona.MISHEARINGS if args.wake_word == persona.WAKE_WORD else ()
        ),
        language=args.language,
        fp16=device in ("cuda", "mps"),
        question_timeout=args.question_timeout,
        max_no_speech=args.wake_confidence,
        on_wake_command=args.on_wake,
        wake_reply=speak,
        muted_until=muted_until,
        on_question=(
            answer_aloud(brain, talker, log, registry, runner) if brain else None
        ),
        acknowledge=acknowledge,
        greeting=greeting,
    )


def run_plain(args) -> int:
    log = HitLog(
        args.wake_word,
        path=args.log,
        verbose=args.verbose,
        bell=not args.no_bell,
    )
    try:
        listener = assemble(args, log, lambda t: print(f"[setup] {t} …", flush=True))
    except model.ModelError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return listener.run()


def run_tui(args) -> int:
    import ui

    def build(app: "ui.AssistantApp") -> Listener:
        log = ui.UiLog(
            app,
            args.wake_word,
            path=args.log,
            verbose=args.verbose,
            bell=False,
        )
        return assemble(args, log, log.setup)

    app = ui.AssistantApp(build, wake_word=args.wake_word)
    try:
        app.run()
    finally:
        app.shutdown_listener()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.list_devices:
        print(audio.list_devices())
        return 0

    return run_plain(args) if args.plain else run_tui(args)


if __name__ == "__main__":
    sys.exit(main())
