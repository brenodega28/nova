"""What the dashboard is allowed to change, and where the change is kept.

    from settings import load, store

    current = load()
    current.voice                      # 'en_US-ryan-medium', unless changed
    store().write({"voice": "en_US-amy-medium"})

Every value here already has a home — :data:`persona.VOICE`, :data:`audio.SENSITIVITY`,
:data:`listener.QUESTION_MODEL` — and that home stays the one place the default is
written down. This module adds a second, narrower thing: a curated list of which
of those a dashboard may change, what it may change them to, and a database
holding only the differences. A value nobody has touched is not stored at all, so
the constants remain the answer to "what does she do out of the box" and SQLite
only ever answers "what did the user change".

The list is curated on purpose. Every field is a control somebody has to
understand on a phone screen, so it carries the label and help text it will be
shown with, and bounds it cannot be driven outside of. Constants not named here —
block sizes, pre-roll, ANSI escapes — are not settings, they are implementation,
and exposing them would mean defending values nobody should be choosing.

``restart_required`` marks the fields that only take effect when the listener is
rebuilt, which is most of them: a Whisper model already resident on the GPU does
not change because a row changed. :mod:`control` reports it back so the dashboard
can say so rather than leaving the user wondering why nothing happened.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

import audio
import hit_log
import listener
import persona

DB_PATH = Path(__file__).resolve().parent / "nova.db"

WHISPER_MODELS = ("tiny.en", "base.en", "small.en", "medium.en", "turbo", "large")


def _control(
    default: Any,
    label: str,
    help: str,
    group: str,
    restart_required: bool = True,
    **constraints: Any,
) -> Any:
    """Declare one setting, with everything a dashboard needs to render it."""
    return Field(
        default=default,
        title=label,
        description=help,
        json_schema_extra={"group": group, "restart_required": restart_required},
        **constraints,
    )


class Settings(BaseSettings):
    """Every setting the dashboard may change, with the constants as defaults."""

    voice: str = _control(
        persona.VOICE,
        "Voice",
        "The Piper voice she speaks with. Downloaded on first use.",
        "Voice",
    )
    greeting: str = _control(
        persona.GREETING,
        "Greeting",
        "Spoken once the microphone is calibrated. Empty for silence.",
        "Voice",
    )
    wake_reply: str = _control(
        persona.WAKE_REPLY,
        "Wake reply",
        "Spoken the instant the wake word lands. Empty for silence.",
        "Voice",
    )

    llm_model: str = _control(
        persona.MODEL,
        "Language model",
        "The Ollama model that answers questions.",
        "Thinking",
        min_length=1,
        max_length=128,
    )
    llm_host: str = _control(
        persona.HOST,
        "Ollama host",
        "Where Ollama is listening.",
        "Thinking",
        min_length=1,
        max_length=256,
    )
    think: bool = _control(
        persona.THINK,
        "Show reasoning",
        "Let the model reason before answering. More accurate, and slower.",
        "Thinking",
    )
    history_turns: int = _control(
        persona.HISTORY_TURNS,
        "Remembered turns",
        "How many past exchanges she is reminded of.",
        "Thinking",
        ge=0,
        le=50,
    )

    wake_model: str = _control(
        listener.WAKE_MODEL,
        "Wake model",
        "The small Whisper model that spots the wake word.",
        "Hearing",
    )
    question_model: str = _control(
        listener.QUESTION_MODEL,
        "Question model",
        "The Whisper model that transcribes the question. Slower is more accurate.",
        "Hearing",
    )
    language: str = _control(
        listener.LANGUAGE,
        "Language",
        "Spoken language, or 'auto' to detect it each time.",
        "Hearing",
        min_length=2,
        max_length=16,
    )
    question_timeout: float = _control(
        listener.QUESTION_TIMEOUT,
        "Question timeout",
        "Seconds to wait for a question before going back to sleep.",
        "Hearing",
        ge=1.0,
        le=60.0,
    )
    wake_confidence: float = _control(
        listener.WAKE_CONFIDENCE,
        "Wake confidence",
        "Reject wakes Whisper scores above this as non-speech. Lower is stricter.",
        "Hearing",
        ge=0.0,
        le=1.0,
    )

    sensitivity: float = _control(
        audio.SENSITIVITY,
        "Sensitivity",
        "Gate height, as a multiple of the measured noise floor.",
        "Microphone",
        ge=1.0,
        le=20.0,
    )
    silence_seconds: float = _control(
        audio.SILENCE_SECONDS,
        "Silence",
        "Seconds of quiet that end an utterance.",
        "Microphone",
        ge=0.05,
        le=3.0,
    )
    max_utterance: float = _control(
        audio.MAX_UTTERANCE,
        "Longest utterance",
        "Force a transcription after this many seconds of speech.",
        "Microphone",
        ge=1.0,
        le=120.0,
    )
    input_device: str = _control(
        audio.INPUT_DEVICE or "",
        "Microphone",
        "Input device name or index. Empty for the system default.",
        "Microphone",
    )

    verbose: bool = _control(
        hit_log.VERBOSE,
        "Verbose log",
        "Record every wake scan, including the rejected ones.",
        "Diagnostics",
        restart_required=False,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Values passed in win, then the database, then the constants."""
        return (init_settings, _Stored(settings_cls))


class _Stored(PydanticBaseSettingsSource):
    """The overrides table, as a settings source Pydantic can read."""

    def get_field_value(self, field, field_name: str):
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        known = set(self.settings_cls.model_fields)
        return {k: v for k, v in store().read().items() if k in known}


class Store:
    """The differences from the defaults, in one table of one SQLite file.

    Values go in as JSON rather than as columns, because the alternative is a
    migration every time a setting is added and this table is never queried by
    anything but "give me all of it". A row that will not parse is dropped rather
    than raised: a corrupt override should cost the user the value they set, not
    the ability to start.
    """

    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._prepare()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5.0)

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings ("
                "  name TEXT PRIMARY KEY,"
                "  value TEXT NOT NULL,"
                "  changed_at REAL NOT NULL"
                ")"
            )

    def read(self) -> dict[str, Any]:
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT name, value FROM settings").fetchall()
        kept = {}
        for name, value in rows:
            try:
                kept[name] = json.loads(value)
            except ValueError:
                continue
        return kept

    def write(self, changes: dict[str, Any]) -> "Settings":
        """Check the whole set against the model, then keep it. All or nothing.

        Validating the merged result rather than each value on its own is what
        makes a bad change cost nothing: the row is only written once something
        that parses as a complete, legal settings object has been built from it.
        """
        merged = {**self.read(), **changes}
        checked = Settings(**merged)

        with self._lock, self._connect() as db:
            db.executemany(
                "INSERT INTO settings (name, value, changed_at) VALUES (?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value, "
                "changed_at = excluded.changed_at",
                [
                    (name, json.dumps(getattr(checked, name)), time.time())
                    for name in changes
                ],
            )
        return checked

    def clear(self, names: list[str] | None = None) -> "Settings":
        """Forget an override, so the constant answers again."""
        with self._lock, self._connect() as db:
            if names is None:
                db.execute("DELETE FROM settings")
            else:
                db.executemany(
                    "DELETE FROM settings WHERE name = ?", [(n,) for n in names]
                )
        return load()


_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store()
    return _store


def load() -> Settings:
    """The settings as they stand: the constants, with the overrides applied."""
    return Settings()


def describe() -> dict[str, Any]:
    """Every setting, as a dashboard needs to see it.

    The JSON Schema Pydantic already builds from the field declarations is the
    same one that validates a write, so what the dashboard offers and what the
    backend accepts cannot drift apart.
    """
    schema = Settings.model_json_schema()
    overridden = set(store().read())
    current = load()
    return {
        "schema": schema,
        "values": {
            name: {
                "value": getattr(current, name),
                "default": field.default,
                "overridden": name in overridden,
            }
            for name, field in Settings.model_fields.items()
        },
    }
