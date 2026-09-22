#!/usr/bin/env python3
"""Entry point: listen for the wake word, answer the question out loud.

    uv run main.py

There is nothing to configure here and no command line to keep in step. Every
choice lives with the thing it governs — who she is and which model she thinks
with in :mod:`persona`, how the microphone is read in :mod:`audio`, which Whisper
models run and how long she waits in :mod:`listener`, what gets logged in
:mod:`hit_log` — so each one has exactly one home.

Set :data:`INTERFACE` to ``False`` for scrolling console output instead of the
full-screen interface, which is easier to read when something is going wrong.
"""

from __future__ import annotations

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
from listener import QUESTION_MODEL, WAKE_MODEL, Listener

INTERFACE = True
STICKY_SECONDS = 120.0


def resolve_device() -> str:
    """The fastest thing torch can see: a GPU where there is one, else the CPU."""
    import torch

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


def assemble(log: HitLog, progress: Callable[[str], None]) -> Listener:
    """Load everything and wire it together, reporting progress as it goes.

    Called on the main thread in console mode and from the interface's worker
    thread otherwise, which is why every word of progress goes through ``log``
    rather than straight to stdout.
    """
    device = resolve_device()
    if device == "mps":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    import talk

    talker = talk.configure(voice=persona.VOICE)
    progress(f"voice '{persona.VOICE}'")
    talker.load()

    greeting = None
    if persona.GREETING:
        greeting = lambda: talker.say(persona.GREETING, blocking=False)
    speak = None
    if persona.WAKE_REPLY:
        speak = lambda: talker.say(persona.WAKE_REPLY, blocking=False)
    acknowledge = None
    if persona.THINKING_REPLIES:
        acknowledge = lambda: talker.say(
            random.choice(persona.THINKING_REPLIES), blocking=False
        )

    brain = model.Model()
    progress(f"llm '{persona.MODEL}' at {persona.HOST}")
    brain.load()

    registry = broker.Registry()
    runner = broker.Runner(python=sys.executable)
    for complaint in registry.broken:
        log.error(f"module skipped — {complaint}")
    if registry.modules:
        progress(f"modules {', '.join(sorted(registry.modules))}")

    lend_tqdm_a_plain_lock()
    import whisper

    progress(f"wake model '{WAKE_MODEL}' on {device}")
    wake_model = whisper.load_model(WAKE_MODEL, device=device)
    progress(f"question model '{QUESTION_MODEL}' on {device}")
    question_model = whisper.load_model(QUESTION_MODEL, device=device)

    return Listener(
        wake_model,
        question_model,
        capture=audio.AudioCapture(),
        log=log,
        fp16=device in ("cuda", "mps"),
        wake_reply=speak,
        muted_until=talker.muted_until,
        on_question=answer_aloud(brain, talker, log, registry, runner),
        acknowledge=acknowledge,
        greeting=greeting,
    )


def run_console() -> int:
    log = HitLog(persona.WAKE_WORD)
    try:
        listener = assemble(log, lambda t: print(f"[setup] {t} …", flush=True))
    except model.ModelError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return listener.run()


def run_interface() -> int:
    import ui

    def build(app: "ui.AssistantApp") -> Listener:
        log = ui.UiLog(app, persona.WAKE_WORD, bell=False)
        return assemble(log, log.setup)

    app = ui.AssistantApp(build)
    try:
        app.run()
    finally:
        app.shutdown_listener()
    return 0


def main() -> int:
    return run_interface() if INTERFACE else run_console()


if __name__ == "__main__":
    sys.exit(main())
