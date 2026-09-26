from __future__ import annotations

from typing import Any

import languages
import settings as settings_module
import talk


def voice(name: str) -> None:
    talk.fetch_voice(name)
    settings_module.store().write({"voice": name})


def language(code: str) -> dict[str, Any]:
    languages.name_of(code)
    changes = languages.changes_for(code, settings_module.load())
    talk.fetch_voice(changes["voice"])
    settings_module.store().write(changes)
    if code == languages.ENGLISH:
        settings_module.store().clear(list(languages.TRANSLATED))
    return changes
