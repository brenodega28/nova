"""Wake-word detection and the conversation state machine.

Two models run side by side. A small one scans rolling partials while the
speaker is still talking, so the wake word is flagged within a fraction of a
second.
A large one transcribes the question that follows, where accuracy matters and
a little latency does not.

Capture runs on whichever thread calls :meth:`Listener.run`, wake scanning on a
second, and the question transcription on a third. The wake scanner keeps only
the newest partial, so a slow pass never leaves it working through stale audio.

Interrupt handling is installed only when :meth:`Listener.run` is called on the
main thread, since signals cannot be registered anywhere else — under a UI that
owns the main thread, quitting is the UI's job.
"""

from __future__ import annotations

import difflib
import queue
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Sequence

import persona
from audio import AudioCapture, Utterance
from hit_log import HitLog

IDLE = "idle"
AWAITING_QUESTION = "awaiting_question"


class WakeWordMatcher:
    """Spot the wake word, tolerating the ways Whisper tends to mangle it.

    ``variants`` are the near misses worth accepting outright; which ones those
    are depends on how the name sounds, so they are the persona's business rather
    than this class's. Anything not listed can still land on the fuzzy fallback.
    """

    def __init__(
        self, wake_word: str, variants: Sequence[str] = (), fuzz: float = 0.82
    ):
        self.wake_word = wake_word.lower().strip()
        self.fuzz = fuzz
        spellings = [self.wake_word, *(v.lower().strip() for v in variants if v)]
        self._pattern = re.compile(
            r"\b(" + "|".join(re.escape(v) for v in spellings) + r")\b"
        )

    @staticmethod
    def normalize(text: str) -> str:
        text = text.lower().replace("’", "'")
        text = re.sub(r"[^a-z0-9'\s]", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def find(self, text: str) -> tuple[int, int] | None:
        """Return the (start, end) span of the wake word in normalized text."""
        normalized = self.normalize(text)
        if not normalized:
            return None

        match = self._pattern.search(normalized)
        if match:
            return match.span()

        for word_match in re.finditer(r"[a-z']+", normalized):
            ratio = difflib.SequenceMatcher(
                None, word_match.group(), self.wake_word
            ).ratio()
            if ratio >= self.fuzz:
                return word_match.span()
        return None

    def trailing(self, text: str) -> str:
        """Whatever was said after the wake word, if anything."""
        span = self.find(text)
        if span is None:
            return ""
        return self.normalize(text)[span[1]:].strip(" ,.!?")


class LatestSlot:
    """A one-item mailbox: a new put replaces whatever has not been taken yet."""

    def __init__(self):
        self._item = None
        self._closed = False
        self._ready = threading.Condition()

    def put(self, item) -> None:
        with self._ready:
            self._item = item
            self._ready.notify()

    def get(self):
        with self._ready:
            while self._item is None and not self._closed:
                self._ready.wait()
            item, self._item = self._item, None
            return item

    def close(self) -> None:
        with self._ready:
            self._closed = True
            self._ready.notify_all()


class Listener:
    """Flags the wake word, then transcribes the question that follows it."""

    def __init__(
        self,
        wake_model,
        question_model,
        capture: AudioCapture,
        log: HitLog,
        wake_word: str = persona.WAKE_WORD,
        wake_variants: Sequence[str] = persona.MISHEARINGS,
        language: str | None = "en",
        fp16: bool = False,
        question_timeout: float = 10.0,
        rearm_after: float = 1.5,
        max_no_speech: float = 0.5,
        on_wake_command: str | None = None,
        wake_reply: Callable[[], None] | None = None,
        muted_until: Callable[[], float] | None = None,
        on_question: Callable[[str], None] | None = None,
        acknowledge: Callable[[], None] | None = None,
        greeting: Callable[[], None] | None = None,
    ):
        self.wake_model = wake_model
        self.question_model = question_model
        self.capture = capture
        self.log = log
        self.matcher = WakeWordMatcher(wake_word, wake_variants)
        self.language = language
        self.fp16 = fp16
        self.question_timeout = question_timeout
        self.rearm_after = rearm_after
        self.max_no_speech = max_no_speech
        self.on_wake_command = on_wake_command
        self.wake_reply = wake_reply
        self.muted_until = muted_until or (lambda: 0.0)
        self.on_question = on_question
        self.acknowledge = acknowledge
        self.greeting = greeting

        self.partials = LatestSlot()
        self.utterances: queue.Queue[Utterance | None] = queue.Queue()
        self.stop_event = threading.Event()

        self._lock = threading.Lock()
        self._state = IDLE
        self._deadline = 0.0
        self._wake_burst = 0.0
        self._last_wake_at = 0.0

    def _run(self, model, utterance: Utterance) -> dict:
        return model.transcribe(
            utterance.audio,
            language=self.language,
            task="transcribe",
            fp16=self.fp16,
            temperature=0.0,
            condition_on_previous_text=False,
            initial_prompt=f"{self.matcher.wake_word.capitalize()}.",
        )

    def _transcribe(self, model, utterance: Utterance) -> str:
        return self._run(model, utterance)["text"].strip()

    @staticmethod
    def _no_speech(result: dict) -> float:
        """How confident Whisper is that the clip holds no speech at all."""
        return max((s["no_speech_prob"] for s in result["segments"]), default=1.0)

    def on_partial(self, utterance: Utterance) -> None:
        if utterance.captured_at <= self.muted_until():
            return
        with self._lock:
            if self._state != IDLE:
                return
        self.partials.put(utterance)

    def wake_loop(self) -> None:
        while not self.stop_event.is_set():
            utterance = self.partials.get()
            if utterance is None:
                return
            with self._lock:
                stale = (
                    self._state != IDLE
                    or utterance.captured_at - self._last_wake_at < self.rearm_after
                )
            if stale:
                continue
            try:
                result = self._run(self.wake_model, utterance)
            except Exception as exc:
                self.log.error(f"wake scan failed: {exc}")
                continue
            text = result["text"].strip()
            if not text:
                continue
            if self._no_speech(result) > self.max_no_speech:
                self.log.scanned(utterance, f"{text}  (no_speech)")
                continue
            if self.matcher.find(text) is None:
                self.log.scanned(utterance, text)
                continue
            self._fire(utterance, text)

    def _fire(self, utterance: Utterance, text: str) -> None:
        now = time.time()
        with self._lock:
            if self._state != IDLE:
                return
            self._state = AWAITING_QUESTION
            self._deadline = now + self.question_timeout
            self._wake_burst = utterance.started_at
            self._last_wake_at = now

        self.log.wake(utterance, text, latency=now - utterance.captured_at)
        if self.wake_reply:
            try:
                self.wake_reply()
            except Exception as exc:
                self.log.error(f"wake reply failed: {exc}")
        if self.on_wake_command:
            try:
                subprocess.Popen(
                    self.on_wake_command,
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as exc:
                self.log.error(f"on-wake command failed: {exc}")

    def _spoken_past_wake_word(self, utterance: Utterance) -> bool:
        """Did the burst that woke us carry a question of its own?

        The partial that fired only covered the audio up to that instant, so
        the burst is re-scanned with the cheap model once it is complete.
        """
        try:
            return bool(self.matcher.trailing(self._transcribe(self.wake_model, utterance)))
        except Exception as exc:
            self.log.error(f"wake burst rescan failed: {exc}")
            return False

    def _on_ready(self, floor: float, threshold: float) -> None:
        """Announce that the microphone is live and the gate is calibrated.

        The greeting waits for calibration rather than leading it: measuring the
        noise floor while she is talking would set the gate above her own voice
        and leave her deaf to the wake word.
        """
        self.log.ready(floor, threshold)
        if not self.greeting:
            return
        try:
            self.greeting()
        except Exception as exc:
            self.log.error(f"greeting failed: {exc}")

    def _acknowledge(self) -> None:
        """Say something the moment a question lands, before transcribing it.

        The large model takes a second or more and the answer takes longer
        still, which is a long time to sit in silence wondering whether you were
        heard at all. This fires first and plays while that work happens.
        """
        if not self.acknowledge:
            return
        try:
            self.acknowledge()
        except Exception as exc:
            self.log.error(f"acknowledgement failed: {exc}")

    def question_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                utterance = self.utterances.get(timeout=0.25)
            except queue.Empty:
                self._expire()
                continue
            if utterance is None:
                return
            try:
                self._consider(utterance)
            except Exception as exc:
                self.log.error(f"transcription failed: {exc}")

    def _expire(self) -> None:
        with self._lock:
            if self._state != AWAITING_QUESTION or time.time() < self._deadline:
                return
            self._state = IDLE
        self.log.timeout()

    def _consider(self, utterance: Utterance) -> None:
        if utterance.started_at <= self.muted_until():
            return
        with self._lock:
            if self._state != AWAITING_QUESTION:
                return
            if time.time() > self._deadline:
                self._state = IDLE
                self.log.timeout()
                return
            is_wake_burst = utterance.started_at == self._wake_burst

        if is_wake_burst and not self._spoken_past_wake_word(utterance):
            return

        self._acknowledge()

        text = self._transcribe(self.question_model, utterance)
        if not text:
            return

        if is_wake_burst:
            text = self.matcher.trailing(text) or text

        with self._lock:
            self._state = IDLE
        self.log.question(utterance, text)

        if self.on_question:
            try:
                self.on_question(text)
            except Exception as exc:
                self.log.error(f"answering failed: {exc}")

    def run(self) -> int:
        workers = [
            threading.Thread(target=self.wake_loop, daemon=True),
            threading.Thread(target=self.question_loop, daemon=True),
        ]
        for worker in workers:
            worker.start()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, lambda *_: self.stop_event.set())
        try:
            self.capture.run(
                self.utterances.put,
                self.stop_event,
                on_ready=self._on_ready,
                on_partial=self.on_partial,
            )
        finally:
            self.stop_event.set()
            self.partials.close()
            self.utterances.put(None)
            for worker in workers:
                worker.join(timeout=30)

        self.log.summary()
        return 0
