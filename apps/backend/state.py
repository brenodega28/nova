"""What she is doing right now, held somewhere a second screen can read it.

    from state import State, StateLog

    state = State()
    log = StateLog(state, persona.WAKE_WORD)
    ...
    state.snapshot()        # everything a client that just connected has missed

The interface has always known this — which face to draw, what was said, how long
it took — but it knew it as widgets, and a widget is only legible to the terminal
it is drawn in. A dashboard connecting halfway through a conversation cannot be
caught up by an event stream alone: events say what changed, and it needs to know
what *is*.

So the same facts are kept a second time, as plain data. :class:`StateLog` is a
:class:`HitLog` like :class:`ui.UiLog` is, subscribed to exactly the same calls,
which is what keeps the two from disagreeing: neither is derived from the other,
both are driven by the listener itself.

The conversation is capped. This is a record of a session for something drawing a
screen with it, not a transcript, and a process that runs for a week should not
grow a list all week.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from audio import Utterance
from hit_log import HitLog

LOADING = "loading"
IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"
BROKEN = "broken"

REMEMBERED_TURNS = 50
REMEMBERED_NOTES = 50


class State:
    """The current picture, guarded by a lock because two threads write it.

    Every reader gets a copy. Handing out the live structures would mean a client
    serialising a conversation while the listener appends to it, and the cost of
    copying something this small is not worth the race.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.started_at = time.time()
        self.activity = LOADING
        self.setup: list[str] = []
        self.notes: deque[dict] = deque(maxlen=REMEMBERED_NOTES)
        self.turns: deque[dict] = deque(maxlen=REMEMBERED_TURNS)
        self.wakes = 0
        self.noise_floor: float | None = None
        self.gate: float | None = None
        self.last_error: str | None = None
        self.speaking_text = ""

    def _note(self, kind: str, text: str) -> None:
        self.notes.append({"kind": kind, "text": text, "at": time.time()})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "activity": self.activity,
                "started_at": self.started_at,
                "uptime_seconds": round(time.time() - self.started_at, 1),
                "setup": list(self.setup),
                "notes": list(self.notes),
                "turns": list(self.turns),
                "wakes": self.wakes,
                "noise_floor": self.noise_floor,
                "gate": self.gate,
                "last_error": self.last_error,
            }


class StateLog(HitLog):
    """A :class:`HitLog` that writes down what happened instead of showing it."""

    def __init__(self, state: State, wake_word: str, **kwargs):
        super().__init__(wake_word, **kwargs)
        self.state = state

    def setup(self, text: str) -> None:
        with self.state._lock:
            self.state.setup.append(text)
            self.state._note("setup", text)

    def ready(self, floor: float, threshold: float) -> None:
        with self.state._lock:
            self.state.activity = IDLE
            self.state.noise_floor = round(floor, 5)
            self.state.gate = round(threshold, 5)
            self.state._note("system", f"microphone live, noise floor {floor:.4f} rms")

    def scanned(self, utterance: Utterance, text: str) -> None:
        if self.verbose:
            with self.state._lock:
                self.state._note("scan", text)

    def wake(self, utterance: Utterance, text: str, latency: float) -> None:
        self.hits += 1
        with self.state._lock:
            self.state.activity = LISTENING
            self.state.wakes = self.hits
            self.state._note("wake", f"woke on '{self.wake_word}' #{self.hits}")
        self._append("wake", utterance, text, latency=latency)

    def question(self, utterance: Utterance, text: str) -> None:
        with self.state._lock:
            self.state.speaking_text = ""
            self.state.turns.append(
                {"who": "you", "text": text, "at": time.time(), "seconds": None}
            )
        self._append("question", utterance, text)

    def thinking(self) -> None:
        with self.state._lock:
            self.state.activity = THINKING

    def answer_chunk(self, text: str) -> None:
        """Grow the answer in place, the way the screen does.

        The answer arrives a sentence at a time and is spoken as it does, so a
        client that connects mid-answer should see the part already said rather
        than nothing until the last sentence lands.
        """
        with self.state._lock:
            self.state.activity = SPEAKING
            self.state.speaking_text = f"{self.state.speaking_text} {text}".strip()
            said = self.state.speaking_text
            if self.state.turns and self.state.turns[-1]["who"] == self.wake_word:
                self.state.turns[-1]["text"] = said
            else:
                self.state.turns.append(
                    {
                        "who": self.wake_word,
                        "text": said,
                        "at": time.time(),
                        "seconds": None,
                    }
                )

    def answer(self, text: str, elapsed: float) -> None:
        with self.state._lock:
            self.state.activity = IDLE
            self.state.speaking_text = ""
            if self.state.turns and self.state.turns[-1]["who"] == self.wake_word:
                self.state.turns[-1]["text"] = text
                self.state.turns[-1]["seconds"] = round(elapsed, 2)
        self._append("answer", None, text, latency=elapsed)

    def used(self, module: str, tool: str, seconds: float) -> None:
        with self.state._lock:
            self.state._note("module", f"{module}.{tool} ({seconds:.1f}s)")
        self._append("module", None, f"{module}.{tool}", latency=seconds)

    def timeout(self) -> None:
        with self.state._lock:
            self.state.activity = IDLE
            self.state._note("system", "no question — back to listening")

    def error(self, message: str) -> None:
        with self.state._lock:
            self.state.last_error = message
            self.state._note("error", message)

    def summary(self) -> None:
        with self.state._lock:
            self.state._note("system", f"heard '{self.wake_word}' {self.hits} time(s)")

    def broken(self, message: str) -> None:
        """Setup failed. Nothing is listening, and the dashboard should say so."""
        with self.state._lock:
            self.state.activity = BROKEN
            self.state.last_error = message
            self.state._note("error", message)
