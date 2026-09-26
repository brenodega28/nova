"""Speech out: Piper neural text to speech, played straight to the speakers.

    import talk
    talk.Talker().say("Yes?")

Synthesis is roughly fifty times faster than real time, so a short reply starts
playing within a couple of hundred milliseconds of the call.

The microphone hears whatever comes out of the speakers, so a listener needs to
know when to stop trusting its input. :meth:`Talker.muted_until` reports the moment
audio becomes trustworthy again — while a reply is playing it is infinite, and
once playback ends it is the end time plus a short settling margin.

Only one thing is ever audible at a time. A reply fired off with
``blocking=False`` while another is still playing waits its turn rather than
talking over it, which is what lets an acknowledgement overlap the work it is
covering for without colliding with the answer that follows.
"""

from __future__ import annotations

import atexit
import json
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

import numpy as np
import sounddevice as sd

import persona

DEFAULT_VOICE = persona.VOICE
DEFAULT_DIR = Path.home() / ".cache" / "piper"
SETTLE_SECONDS = 0.3
SLICE_SECONDS = 0.05
SHUTDOWN_SECONDS = 2.0
CHIME_RATE = 22050
CHIME_TONES = (880.0, 1320.0)
CHIME_TONE_SECONDS = 0.06
CHIME_VOLUME = 0.25


def chime_samples() -> np.ndarray:
    t = np.arange(int(CHIME_RATE * CHIME_TONE_SECONDS)) / CHIME_RATE
    envelope = np.sin(np.pi * t / CHIME_TONE_SECONDS)
    tones = [np.sin(2 * np.pi * pitch * t) * envelope for pitch in CHIME_TONES]
    return (np.concatenate(tones) * CHIME_VOLUME).astype(np.float32)


def catalog() -> dict:
    from piper.download_voices import VOICES_JSON

    with urlopen(VOICES_JSON) as response:
        return json.load(response)


def available_voices(language: str) -> list[str]:
    wanted = language.lower()
    return sorted(voice for voice in catalog() if speaks(voice, wanted))


def fetch_voice(voice: str, download_dir: Path = DEFAULT_DIR) -> None:
    from piper.download_voices import download_voice

    if voice not in catalog():
        raise ValueError(f"no voice named '{voice}' — see /list-voices")
    download_dir.mkdir(parents=True, exist_ok=True)
    download_voice(voice, download_dir)


def speaks(voice: str, language: str) -> bool:
    locale = voice.split("-")[0].lower()
    return language in (locale, locale.split("_")[0])


class Talker:
    """A loaded Piper voice that can speak, and say whether it is speaking."""

    def __init__(
        self,
        voice: str = DEFAULT_VOICE,
        download_dir: str | Path = DEFAULT_DIR,
        settle: float = SETTLE_SECONDS,
        output_device: str | int | None = None,
    ):
        self.voice_name = voice
        self.download_dir = Path(download_dir).expanduser()
        self.settle = settle
        self.output_device = output_device

        self._voice = None
        self._load_lock = threading.Lock()
        self._play_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._speakers = 0
        self._quiet_since = 0.0
        self._interrupt = threading.Event()
        self._threads: list[threading.Thread] = []
        self._draining = False

    def _model_path(self) -> Path:
        path = self.download_dir / f"{self.voice_name}.onnx"
        if not path.exists():
            from piper.download_voices import download_voice

            self.download_dir.mkdir(parents=True, exist_ok=True)
            download_voice(self.voice_name, self.download_dir)
        return path

    def load(self):
        """Load the model, downloading the voice first if it is missing."""
        with self._load_lock:
            if self._voice is None:
                from piper import PiperVoice

                self._voice = PiperVoice.load(self._model_path())
            return self._voice

    def is_speaking(self) -> bool:
        with self._state_lock:
            return self._speakers > 0

    def muted_until(self) -> float:
        """Timestamp after which microphone audio is trustworthy again."""
        with self._state_lock:
            if self._speakers:
                return float("inf")
            return self._quiet_since + self.settle

    def stop(self) -> None:
        """Cut off whatever is playing."""
        self._interrupt.set()

    def chime(self) -> None:
        sd.play(chime_samples(), CHIME_RATE, device=self.output_device)

    def say(self, text: str, blocking: bool = True) -> None:
        text = text.strip()
        if not text:
            return
        if blocking:
            self._speak(text)
            return
        thread = threading.Thread(target=self._speak, args=(text,), daemon=True)
        with self._state_lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
            if not self._draining:
                self._draining = True
                atexit.register(self._drain)
        thread.start()

    def wait(self, timeout: float | None = None) -> None:
        """Block until nothing queued with ``blocking=False`` is still playing."""
        with self._state_lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=timeout)

    def _drain(self) -> None:
        """Cut playback short and let the audio thread unwind before exit.

        Tearing the interpreter down underneath PortAudio mid-write crashes the
        process, so a reply still playing has to be stopped and joined rather
        than abandoned as a daemon thread.
        """
        self.stop()
        self.wait(timeout=SHUTDOWN_SECONDS)

    def _speak(self, text: str) -> None:
        voice = self.load()
        with self.speaking(), self._play_lock:
            self._interrupt.clear()
            stream = sd.OutputStream(
                samplerate=voice.config.sample_rate,
                channels=1,
                dtype="int16",
                device=self.output_device,
            )
            try:
                stream.start()
                step = max(1, int(voice.config.sample_rate * SLICE_SECONDS))
                for chunk in voice.synthesize(text):
                    samples = np.asarray(chunk.audio_int16_array, dtype=np.int16)
                    for start in range(0, len(samples), step):
                        if self._interrupt.is_set():
                            return
                        stream.write(samples[start:start + step])
            finally:
                stream.stop()
                stream.close()

    @contextmanager
    def speaking(self):
        """Hold the muted state open around something that makes noise.

        Nesting is what makes a multi-sentence answer safe: the gate stays shut
        through the gaps between sentences, not just while audio is playing.
        """
        with self._state_lock:
            self._speakers += 1
        try:
            yield
        finally:
            with self._state_lock:
                self._speakers -= 1
                if not self._speakers:
                    self._quiet_since = time.time()


if __name__ == "__main__":
    import sys

    Talker().say(" ".join(sys.argv[1:]) or "Yes? I am listening.")
