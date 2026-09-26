from __future__ import annotations

import os
import sys
import threading
import time

import settings as settings_module


def describe(supervisor) -> dict:
    chosen = settings_module.load()
    listener = getattr(supervisor, "listener", None)
    return {
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "threads": threading.active_count(),
        "generation": getattr(supervisor, "generation", 0),
        "running": listener is not None,
        "at": time.time(),
        "torch": _torch(),
        "models": [
            _whisper("wake", chosen.wake_model, getattr(listener, "wake_model", None)),
            _whisper(
                "question", chosen.question_model, getattr(listener, "question_model", None)
            ),
            _voice(chosen.voice),
            {
                "role": "llm",
                "engine": "ollama",
                "name": chosen.llm_model,
                "host": chosen.llm_host,
                "loaded": None,
            },
        ],
    }


def _torch() -> dict:
    torch = sys.modules.get("torch")
    if torch is None:
        return {"imported": False}
    described: dict = {"imported": True, "version": torch.__version__, "device": "cpu"}
    if torch.cuda.is_available():
        described.update(
            device="cuda",
            gpu=torch.cuda.get_device_name(0),
            allocated=torch.cuda.memory_allocated(),
            reserved=torch.cuda.memory_reserved(),
        )
    elif torch.backends.mps.is_available():
        described.update(
            device="mps",
            allocated=torch.mps.current_allocated_memory(),
            reserved=torch.mps.driver_allocated_memory(),
        )
    return described


def _whisper(role: str, name: str, weights) -> dict:
    described = {"role": role, "engine": "whisper", "name": name, "loaded": weights is not None}
    if weights is None:
        return described
    parameters = list(weights.parameters())
    described.update(
        parameters=sum(p.numel() for p in parameters),
        bytes=sum(p.numel() * p.element_size() for p in parameters),
        device=str(parameters[0].device) if parameters else None,
        dtype=str(parameters[0].dtype).removeprefix("torch.") if parameters else None,
    )
    return described


def _voice(name: str) -> dict:
    talk = sys.modules.get("talk")
    talker = talk.default_talker() if talk is not None else None
    return {
        "role": "voice",
        "engine": "piper",
        "name": talker.voice_name if talker is not None else name,
        "loaded": talker is not None and talker._voice is not None,
        "speaking": talker.is_speaking() if talker is not None else False,
    }
