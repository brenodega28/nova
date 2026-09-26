from __future__ import annotations

import os

from debug.app import Config, DebugApp


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def config() -> Config:
    return Config(
        host=os.environ.get("NOVA_BACKEND_HOST", "127.0.0.1"),
        port=int(os.environ.get("NOVA_BACKEND_PORT", "8765")),
        token=os.environ.get("NOVA_CONTROL_TOKEN", ""),
        ollama_host=os.environ.get("NOVA_OLLAMA_HOST", "http://localhost:11434"),
        interval=float(os.environ.get("NOVA_DEBUG_INTERVAL", "1.0")),
        memory_limit=float(os.environ.get("NOVA_DEBUG_MEMORY_LIMIT_GB", "12")),
        auto_reload=_flag("NOVA_DEBUG_AUTO_RELOAD", True),
        over_limit_samples=int(os.environ.get("NOVA_DEBUG_OVER_LIMIT_SAMPLES", "5")),
        cooldown=float(os.environ.get("NOVA_DEBUG_COOLDOWN", "180")),
    )


def run() -> None:
    DebugApp(config()).run()


if __name__ == "__main__":
    run()
