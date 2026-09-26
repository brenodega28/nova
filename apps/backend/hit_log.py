"""Where heard speech gets written down: the console and an optional JSONL file.

Named ``hit_log`` rather than ``logging`` so it cannot shadow the stdlib module
for anything else importing from this directory.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from audio import Utterance

LOG_PATH = None
VERBOSE = False
RING_BELL = True

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
BELL = "\a"


class HitLog:
    """Records wake-word hits and the questions that follow them."""

    def __init__(
        self,
        wake_word: str,
        path: str | Path | None = LOG_PATH,
        verbose: bool = VERBOSE,
        bell: bool = RING_BELL,
    ):
        self.wake_word = wake_word
        self.path = Path(path).expanduser() if path else None
        self.verbose = verbose
        self.bell = bell
        self.hits = 0
        self.answering = False

    @staticmethod
    def _clock(utterance: Utterance) -> str:
        return datetime.fromtimestamp(utterance.started_at).strftime("%H:%M:%S")

    def starting(self) -> None:
        """A listener is being built. Anything shown about the last one is stale."""
        print("[setup] starting …", flush=True)

    def setup(self, text: str) -> None:
        """One step of loading finished. The interface shows these too."""
        print(f"[setup] {text} …", flush=True)

    def broken(self, message: str) -> None:
        """Setup failed outright, so nothing is listening."""
        self.error(message)

    def ready(self, floor: float, threshold: float) -> None:
        print(
            f"[listening] noise floor {floor:.4f} rms, "
            f"gate at {threshold:.4f} rms — say "
            f"'{self.wake_word}' (ctrl-c to stop)",
            flush=True,
        )

    def scanned(self, utterance: Utterance, text: str) -> None:
        """A partial the wake scanner rejected — only shown when verbose."""
        if self.verbose:
            print(f"{DIM}  {self._clock(utterance)}  ~ {text}{RESET}")

    def wake(self, utterance: Utterance, text: str, latency: float) -> None:
        self.hits += 1
        bell = BELL if self.bell else ""
        print(
            f"\n{bell}{BOLD}[{self._clock(utterance)}] "
            f"{self.wake_word.upper()} #{self.hits}{RESET} "
            f"{DIM}({latency * 1000:.0f} ms){RESET}",
            flush=True,
        )
        self._append("wake", utterance, text, latency=latency)

    def question(self, utterance: Utterance | None, text: str) -> None:
        print(f"  {BOLD}?{RESET} {text}\n", flush=True)
        self._append("question", utterance, text)

    def thinking(self) -> None:
        print(f"{DIM}  … thinking{RESET}", flush=True)

    def answer_chunk(self, text: str) -> None:
        """One spoken sentence, printed as it goes to the speakers."""
        if not self.answering:
            self.answering = True
            print(f"  {BOLD}>{RESET} ", end="", flush=True)
        print(f"{text} ", end="", flush=True)

    def answer(self, text: str, elapsed: float) -> None:
        self.answering = False
        print(f"{DIM}({elapsed:.1f}s){RESET}\n", flush=True)
        self._append("answer", None, text, latency=elapsed)

    def used(self, module: str, tool: str, seconds: float) -> None:
        """A module ran. Worth a line, and worth a row in the log file."""
        print(f"{DIM}  · {module}.{tool} ({seconds:.1f}s){RESET}", flush=True)
        self._append("module", None, f"{module}.{tool}", latency=seconds)

    def timeout(self) -> None:
        print(f"{DIM}  (no question — back to listening){RESET}\n", flush=True)

    def error(self, message: str) -> None:
        print(f"[error] {message}", file=sys.stderr)

    def summary(self) -> None:
        print(f"\n[done] heard '{self.wake_word}' {self.hits} time(s)")

    def _append(
        self,
        event: str,
        utterance: Utterance | None,
        text: str,
        latency: float | None = None,
    ) -> None:
        if not self.path:
            return
        started_at = utterance.started_at if utterance else time.time()
        entry: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(
                started_at, tz=timezone.utc
            ).isoformat(),
            "event": event,
            "wake_word": self.wake_word,
            "text": text,
        }
        if utterance is not None:
            entry["duration_seconds"] = round(utterance.duration, 2)
        if latency is not None:
            entry["latency_seconds"] = round(latency, 3)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")


class Tee(HitLog):
    """One log that is really several: every call goes to all of them.

    The screen, the state a dashboard reads, and the clients attached to the
    control port all want the same events, and the listener should not have to
    know how many of them there are. It holds one log, as it always has.

    Every method is written out rather than generated, because the base class
    implements most of them: anything clever with ``__getattr__`` is shadowed by
    the inherited method and silently prints to a console nobody is reading.

    A sink that does not implement something is skipped rather than crashing the
    caller, since the base :class:`HitLog` has no ``setup`` or ``broken`` and is
    still a perfectly good sink. One sink raising must not rob the others of the
    event either — a dashboard client that went away mid-write is a normal thing
    to happen, and not a reason for the conversation to stop being drawn.
    """

    def __init__(self, sinks, wake_word: str):
        super().__init__(wake_word)
        self.sinks = list(sinks)

    def _fan(self, name: str, *args, **kwargs) -> None:
        for sink in self.sinks:
            method = getattr(sink, name, None)
            if method is None:
                continue
            try:
                method(*args, **kwargs)
            except Exception:
                continue

    def starting(self) -> None:
        self._fan("starting")

    def setup(self, text: str) -> None:
        self._fan("setup", text)

    def ready(self, floor: float, threshold: float) -> None:
        self._fan("ready", floor, threshold)

    def scanned(self, utterance, text: str) -> None:
        self._fan("scanned", utterance, text)

    def wake(self, utterance, text: str, latency: float) -> None:
        self.hits += 1
        self._fan("wake", utterance, text, latency)

    def question(self, utterance, text: str) -> None:
        self._fan("question", utterance, text)

    def thinking(self) -> None:
        self._fan("thinking")

    def answer_chunk(self, text: str) -> None:
        self._fan("answer_chunk", text)

    def answer(self, text: str, elapsed: float) -> None:
        self._fan("answer", text, elapsed)

    def used(self, module: str, tool: str, seconds: float) -> None:
        self._fan("used", module, tool, seconds)

    def timeout(self) -> None:
        self._fan("timeout")

    def error(self, message: str) -> None:
        self._fan("error", message)

    def summary(self) -> None:
        self._fan("summary")

    def broken(self, message: str) -> None:
        self._fan("broken", message)
