from __future__ import annotations

from typing import TYPE_CHECKING, Any

import model
import persona
import talk

if TYPE_CHECKING:
    from settings import Settings

ENGLISH = "en"
AUTO = "auto"
ENGLISH_ONLY_SIZES = ("tiny", "base", "small", "medium")
TRANSLATED = ("greeting", "thinking_replies")


def name_of(code: str) -> str:
    from whisper.tokenizer import LANGUAGES

    if code not in LANGUAGES:
        raise ValueError(f"unknown language '{code}' — use a code like pt, es or de")
    return LANGUAGES[code]


def system_prompt(code: str) -> str:
    if code in (ENGLISH, AUTO):
        return persona.SYSTEM
    reply = persona.REPLY_LANGUAGE.format(language=name_of(code).title())
    return f"{persona.SYSTEM} {reply}"


def whisper_model_for(name: str, code: str) -> str:
    size = name.removesuffix(".en")
    if code == ENGLISH and size in ENGLISH_ONLY_SIZES:
        return f"{size}.en"
    return size


def voice_for(code: str, current: str) -> str:
    for voice in (current, persona.VOICE):
        if talk.speaks(voice, code):
            return voice
    voices = talk.available_voices(code)
    if not voices:
        raise ValueError(f"no Piper voice speaks '{code}' — see /list-voices")
    medium = [voice for voice in voices if voice.endswith("-medium")]
    return (medium or voices)[0]


def changes_for(code: str, current: Settings) -> dict[str, Any]:
    changes: dict[str, Any] = {
        "language": code,
        "voice": voice_for(code, current.voice),
        "wake_model": whisper_model_for(current.wake_model, code),
        "question_model": whisper_model_for(current.question_model, code),
    }
    if code == ENGLISH:
        return changes

    language = name_of(code).title()
    brain = model.Model(name=current.llm_model, host=current.llm_host)
    changes["greeting"] = brain.translate(persona.GREETING, language)
    changes["thinking_replies"] = [
        brain.translate(reply, language) for reply in persona.THINKING_REPLIES
    ]
    return changes
