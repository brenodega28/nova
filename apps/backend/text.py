from __future__ import annotations

import re


def normalize(said: str) -> str:
    said = said.lower().replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9'\s]", " ", said)).strip()
