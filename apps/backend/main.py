#!/usr/bin/env python3
"""Entry point: listen for the wake word, answer the question out loud.

    uv run main.py

There is nothing to configure here and no command line to keep in step. Every
choice lives with the thing it governs — who she is and which model she thinks
with in :mod:`persona`, how the microphone is read in :mod:`audio`, which Whisper
models run and how long she waits in :mod:`listener`, what gets logged in
:mod:`hit_log` — so each one has exactly one home.

She also opens a control port while she runs, which :mod:`control` describes and
``apps/api`` is the thing that speaks to it. Everything that reads or changes her
from outside goes through there: the screen and the dashboard are handed the same
events by the same log, so neither is the authoritative one.
"""

from __future__ import annotations

import os
import queue
import random
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future

import audio
import broker
import control
import languages
import model
import persona
import settings as settings_module
import state as state_module
import talk
from hit_log import HitLog, Tee
from listener import Listener
from supervisor import Supervisor

CONTROL_PORT = True
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
    talker: talk.Talker,
    log: HitLog,
    registry: broker.Registry,
    runner: broker.Runner,
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
        with talker.speaking():
            log.answer_chunk(text)
            talker.say(text, blocking=True)

    recent: dict = {"module": None, "at": 0.0}

    def route(question: str) -> tuple[broker.Installed | None, bool]:
        """Which module should see this, and how sure are we.

        A follow-up carries none of the words that routed the first question —
        "and in Madrid?" names no weather at all — so when nothing matches and a
        module answered a moment ago, that module gets offered the question. It is
        a guess, and the second return value says so.
        """
        matched = registry.match(question)
        if matched is not None:
            return matched, False
        last = recent["module"]
        if last is not None and time.time() - recent["at"] <= STICKY_SECONDS:
            return last, True
        return None, False

    def from_module(question: str, started: float) -> bool:
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
        with talker.speaking():
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


def in_background(load: Callable[[], object], log: HitLog, what: str) -> Future:
    loaded: Future = Future()

    def run() -> None:
        try:
            loaded.set_result(load())
        except Exception as exc:
            log.error(f"{what} failed to load: {exc}")
            loaded.set_exception(exc)

    threading.Thread(target=run, daemon=True).start()
    return loaded


def onto_device(whisper_model, device: str, gpu_lock: threading.Lock):
    for part in whisper_model.modules():
        if next(part.children(), None) is None:
            with gpu_lock:
                part.to(device)
    with gpu_lock:
        return whisper_model.to(device)


def assemble(log: HitLog, progress: Callable[[str], None]) -> Listener:
    """Load everything and wire it together, reporting progress as it goes.

    Called from the interface's worker thread, which is why every word of
    progress goes through ``log`` rather than straight to stdout.

    The values come from :mod:`settings` rather than from the constants directly.
    That is the same set of numbers either way — a setting nobody has changed is
    the constant — but read through the one place a dashboard is allowed to
    change them, so that what she is built with here and what the dashboard
    reports are never two different answers.
    """
    chosen = settings_module.load()
    device = resolve_device()
    if device == "mps":
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")

    talker = talk.Talker(voice=chosen.voice)
    progress(f"voice '{chosen.voice}'")
    talker.load()

    greeting = None
    if chosen.greeting:
        greeting = lambda: talker.say(chosen.greeting, blocking=False)
    acknowledge = None
    if chosen.thinking_replies:
        acknowledge = lambda: talker.say(
            random.choice(chosen.thinking_replies), blocking=False
        )

    brain = model.Model(
        name=chosen.llm_model,
        host=chosen.llm_host,
        system=languages.system_prompt(chosen.language),
        think=chosen.think,
        history_turns=chosen.history_turns,
    )
    progress(f"llm '{chosen.llm_model}' at {chosen.llm_host}")
    in_background(brain.load, log, "llm warm-up")

    registry = broker.Registry()
    runner = broker.Runner(python=sys.executable)
    for complaint in registry.broken:
        log.error(f"module skipped — {complaint}")
    if registry.modules:
        progress(f"modules {', '.join(sorted(registry.modules))}")

    lend_tqdm_a_plain_lock()
    import whisper

    progress(f"wake model '{chosen.wake_model}' on {device}")
    wake_model = whisper.load_model(chosen.wake_model, device=device)
    progress(f"question model '{chosen.question_model}' on {device}")
    gpu_lock = threading.Lock()
    question_model = in_background(
        lambda: onto_device(
            whisper.load_model(chosen.question_model, device="cpu"), device, gpu_lock
        ),
        log,
        "question model",
    )

    return Listener(
        wake_model,
        question_model,
        capture=audio.AudioCapture(
            sensitivity=chosen.sensitivity,
            silence=chosen.silence_seconds,
            max_utterance=chosen.max_utterance,
            input_device=chosen.input_device or None,
        ),
        log=log,
        language=chosen.language if chosen.language != languages.AUTO else None,
        fp16=device in ("cuda", "mps"),
        gpu_lock=gpu_lock,
        question_timeout=chosen.question_timeout,
        max_no_speech=chosen.wake_confidence,
        wake_reply=talker.chime,
        muted_until=talker.muted_until,
        on_question=answer_aloud(brain, talker, log, registry, runner),
        acknowledge=acknowledge,
        greeting=greeting,
        talker=talker,
    )


def supervised(sinks: list, server) -> Supervisor:
    """Tie the log, the listener and the control port into one running thing.

    The sinks are every place an event has to reach. They all see the same events,
    in the same order, from the one log the listener was handed.
    """
    log = Tee(sinks, persona.WAKE_WORD)
    supervisor = Supervisor(lambda: assemble(log, log.setup), log)
    if server is not None:
        server.supervisor = supervisor
    return supervisor


def open_control(picture) -> control.ControlServer | None:
    """Open the control port, or carry on without one if it will not open.

    A port already in use means another backend is running, and that is worth
    saying plainly — but it is not worth refusing to listen over. She works
    perfectly well with nothing attached; the dashboard is the part that does
    not work, and the message says so.
    """
    if not CONTROL_PORT:
        return None
    server = control.ControlServer(picture)
    try:
        server.start()
    except OSError as exc:
        print(f"[error] control port {control.PORT} unavailable: {exc}", file=sys.stderr)
        return None
    print(f"[setup] control port {control.HOST}:{control.PORT} …", flush=True)
    return server


def main() -> int:
    import ui

    picture = state_module.State()
    server = open_control(picture)

    def build(app: "ui.AssistantApp") -> Supervisor:
        sinks: list = [ui.UiLog(app, persona.WAKE_WORD, bell=False)]
        sinks.append(state_module.StateLog(picture, persona.WAKE_WORD))
        if server is not None:
            sinks.append(control.ControlLog(server, persona.WAKE_WORD))
        return supervised(sinks, server)

    app = ui.AssistantApp(build)
    try:
        app.run()
    finally:
        app.shutdown_listener()
        if server is not None:
            server.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
