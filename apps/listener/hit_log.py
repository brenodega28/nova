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

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"
BELL = "\a"


class HitLog:
    """Records wake-word hits and the questions that follow them."""

    def __init__(
        self,
        wake_word: str,
        path: str | Path | None = None,
        verbose: bool = False,
        bell: bool = True,
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

    def question(self, utterance: Utterance, text: str) -> None:
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
        entry = {
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
