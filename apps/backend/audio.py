"""Microphone capture: turning a live input stream into utterances.

Nothing in here knows about Whisper or wake words — it only decides where
speech starts and stops, and hands each slice on.

Two kinds of slices come out. A *partial* is the tail of a burst of speech that
is still in progress, emitted every ``window_interval`` seconds so a detector
can react before the speaker has finished. A complete utterance is emitted once
the speaker stops. Both carry ``started_at``, the moment their burst of speech
began, so a listener can tell which partials and which utterance belong
together.

How loud counts as speech, how long a gap ends an utterance and how much audio a
rolling window holds are all set here, at the top, because this is the file that
reads the microphone. Change them here rather than passing them down.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
BLOCK_SECONDS = 0.03
PREROLL_SECONDS = 0.5
CALIBRATION_SECONDS = 1.0

SENSITIVITY = 3.0
SILENCE_SECONDS = 0.35
MIN_UTTERANCE = 0.25
MAX_UTTERANCE = 15.0
WINDOW_SECONDS = 1.5
WINDOW_INTERVAL = 0.1
INPUT_DEVICE = None


@dataclass
class Utterance:
    """A slice of speech handed from the capture thread to a model."""

    audio: np.ndarray
    started_at: float
    duration: float
    captured_at: float


class VoiceActivityDetector:
    """Energy gate calibrated against the ambient noise floor.

    Speech opens the gate as soon as one block is loud enough and closes it
    after ``hangover`` seconds of quiet, which keeps short pauses inside a
    sentence from splitting it in two.
    """

    def __init__(self, sensitivity: float, hangover: float, floor_rms: float):
        self.threshold = max(floor_rms * sensitivity, 0.004)
        self.hangover = hangover
        self._quiet_for = 0.0
        self.active = False

    def feed(self, block: np.ndarray, block_seconds: float) -> bool:
        """Return True while the gate is open for this block."""
        rms = float(np.sqrt(np.mean(np.square(block))))
        if rms >= self.threshold:
            self.active = True
            self._quiet_for = 0.0
        elif self.active:
            self._quiet_for += block_seconds
            if self._quiet_for >= self.hangover:
                self.active = False
        return self.active


class AudioCapture:
    """Reads the microphone and emits speech as partials and full utterances."""

    def __init__(
        self,
        *,
        sensitivity: float = SENSITIVITY,
        silence: float = SILENCE_SECONDS,
        min_utterance: float = MIN_UTTERANCE,
        max_utterance: float = MAX_UTTERANCE,
        window: float = WINDOW_SECONDS,
        window_interval: float = WINDOW_INTERVAL,
        input_device: str | int | None = INPUT_DEVICE,
    ):
        self.sensitivity = sensitivity
        self.silence = silence
        self.min_utterance = min_utterance
        self.max_utterance = max_utterance
        self.window = window
        self.window_interval = window_interval
        self.input_device = input_device

    def _calibrate(self, stream: sd.InputStream, block_frames: int) -> float:
        """Measure the noise floor so the gate adapts to the current room."""
        levels = []
        deadline = time.monotonic() + CALIBRATION_SECONDS
        while time.monotonic() < deadline:
            block, _ = stream.read(block_frames)
            levels.append(float(np.sqrt(np.mean(np.square(block[:, 0])))))
        return float(np.median(levels)) if levels else 0.0

    def run(
        self,
        on_utterance: Callable[[Utterance], None],
        stop_event: threading.Event,
        on_ready: Callable[[float, float], None] | None = None,
        on_partial: Callable[[Utterance], None] | None = None,
    ) -> None:
        """Capture until ``stop_event`` is set.

        ``on_ready`` is called once with the measured noise floor and the gate
        threshold, after calibration.
        """
        block_frames = int(SAMPLE_RATE * BLOCK_SECONDS)
        preroll_blocks = max(1, int(PREROLL_SECONDS / BLOCK_SECONDS))
        window_blocks = max(1, int(self.window / BLOCK_SECONDS))
        interval_blocks = max(1, int(self.window_interval / BLOCK_SECONDS))

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=block_frames,
            device=self.input_device,
        ) as stream:
            floor = self._calibrate(stream, block_frames)
            vad = VoiceActivityDetector(
                sensitivity=self.sensitivity,
                hangover=self.silence,
                floor_rms=floor,
            )
            if on_ready:
                on_ready(floor, vad.threshold)

            preroll: list[np.ndarray] = []
            speech: list[np.ndarray] = []
            started_at = 0.0
            since_window = 0

            while not stop_event.is_set():
                block, overflowed = stream.read(block_frames)
                if overflowed:
                    print("[warn] input overflow, dropped a block", file=sys.stderr)
                mono = block[:, 0].copy()

                was_active = vad.active
                active = vad.feed(mono, BLOCK_SECONDS)

                if active:
                    if not was_active:
                        speech = list(preroll)
                        started_at = time.time()
                        since_window = 0
                    speech.append(mono)
                    since_window += 1

                    if on_partial and since_window >= interval_blocks:
                        since_window = 0
                        tail = speech[-window_blocks:]
                        on_partial(
                            Utterance(
                                np.concatenate(tail),
                                started_at,
                                len(tail) * BLOCK_SECONDS,
                                time.time(),
                            )
                        )

                    if len(speech) * BLOCK_SECONDS >= self.max_utterance:
                        self._emit(speech, started_at, on_utterance)
                        speech = []
                        vad.active = False
                elif was_active:
                    self._emit(speech, started_at, on_utterance)
                    speech = []

                preroll.append(mono)
                if len(preroll) > preroll_blocks:
                    preroll.pop(0)

    def _emit(
        self,
        blocks: list[np.ndarray],
        started_at: float,
        on_utterance: Callable[[Utterance], None],
    ) -> None:
        duration = len(blocks) * BLOCK_SECONDS
        if duration < self.min_utterance:
            return
        on_utterance(
            Utterance(np.concatenate(blocks), started_at, duration, time.time())
        )
