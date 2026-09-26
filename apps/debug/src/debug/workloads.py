from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from debug import probes

LLM_RUNNERS = ("llama-server", "ollama_llama_server", "llama-cli", "llamafile", "koboldcpp")
AI_KEYWORDS = (
    "whisper",
    "ollama",
    "llama",
    "lm studio",
    "lmstudio",
    "mlx",
    "piper",
    "vosk",
    "gpt4all",
    "comfyui",
    "diffusion",
    "transformers",
    "vllm",
)
PYTHON_NAMES = ("python", "python3", "Python")
GPU_ACTIVE_NS = 1_000_000
IOREG_ENTRY = re.compile(r"\+-o AGXDeviceUserClient")
IOREG_CREATOR = re.compile(r'"IOUserClientCreator" = "pid (\d+), ')
IOREG_GPU_TIME = re.compile(r'"accumulatedGPUTime"=(\d+)')
MODEL_FLAG = re.compile(r"--model[= ](\S+)")


@dataclass
class Workload:
    pid: int
    kind: str
    name: str
    detail: str
    cpu: float
    memory: int
    threads: int
    gpu: float | None = None
    gpu_memory: int | None = None
    children: list[int] = field(default_factory=list)


class GpuClock:
    def __init__(self):
        self._previous: dict[int, int] = {}
        self._previous_at: float | None = None

    def sample(self) -> dict[int, float]:
        if shutil.which("nvidia-smi"):
            return _nvidia_utilization()
        if sys.platform != "darwin":
            return {}
        totals = _apple_gpu_time()
        now = time.monotonic()
        shares: dict[int, float] = {}
        if self._previous_at is not None:
            elapsed = (now - self._previous_at) * 1e9
            for pid, total in totals.items():
                spent = total - self._previous.get(pid, total)
                if elapsed > 0 and spent >= 0:
                    shares[pid] = min(spent / elapsed * 100, 100.0)
        self._previous, self._previous_at = totals, now
        return shares


def _apple_gpu_time() -> dict[int, int]:
    try:
        output = subprocess.run(
            ["ioreg", "-r", "-c", "AGXDeviceUserClient", "-w", "0"],
            capture_output=True,
            text=True,
            timeout=probes.PROBE_TIMEOUT,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    totals: dict[int, int] = {}
    for entry in IOREG_ENTRY.split(output):
        creator = IOREG_CREATOR.search(entry)
        if creator is None:
            continue
        pid = int(creator.group(1))
        spent = sum(int(found) for found in IOREG_GPU_TIME.findall(entry))
        totals[pid] = totals.get(pid, 0) + spent
    return totals


def _nvidia_utilization() -> dict[int, float]:
    try:
        output = subprocess.run(
            ["nvidia-smi", "pmon", "-c", "1", "-s", "u"],
            capture_output=True,
            text=True,
            timeout=probes.PROBE_TIMEOUT,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    shares: dict[int, float] = {}
    for line in output.splitlines():
        parts = line.split()
        if line.startswith("#") or len(parts) < 4 or not parts[1].isdigit():
            continue
        if parts[3].replace(".", "").isdigit():
            shares[int(parts[1])] = shares.get(int(parts[1]), 0.0) + float(parts[3])
    return shares


def nvidia_memory() -> dict[int, int]:
    if not shutil.which("nvidia-smi"):
        return {}
    try:
        output = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=probes.PROBE_TIMEOUT,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    used: dict[int, int] = {}
    for line in output.strip().splitlines():
        pid, _, mebibytes = (part.strip() for part in line.partition(","))
        if pid.isdigit() and mebibytes.isdigit():
            used[int(pid)] = used.get(int(pid), 0) + int(mebibytes) * 1024 * 1024
    return used


class Scanner:
    def __init__(self, backend_pid: int | None = None):
        self.watcher = probes.Watcher()
        self.gpu_clock = GpuClock()
        self.backend_pid = backend_pid
        self._blob_names: dict[str, str] = {}

    def scan(self, ollama_host: str, loaded: list[dict]) -> list[Workload]:
        gpu = self.gpu_clock.sample()
        vram = nvidia_memory()
        blobs = self._blobs(ollama_host, loaded)
        found: list[Workload] = []
        for process in psutil.process_iter(["pid", "name", "cmdline", "ppid"]):
            if process.pid == psutil.Process().pid:
                continue
            classified = self._classify(process, blobs, loaded, gpu)
            if classified is None:
                continue
            kind, name, detail = classified
            gpu_memory = vram.get(process.pid)
            if kind == "llm":
                gpu_memory = self._llm_vram(blobs, process.info.get("cmdline") or []) or gpu_memory
            sample = self.watcher.sample(process.pid)
            if sample is None:
                continue
            found.append(
                Workload(
                    pid=process.pid,
                    kind=kind,
                    name=name,
                    detail=detail,
                    cpu=sample.cpu,
                    memory=sample.memory,
                    threads=sample.threads,
                    gpu=gpu.get(process.pid),
                    gpu_memory=gpu_memory,
                )
            )
        self.watcher.forget_except({workload.pid for workload in found})
        return sorted(found, key=lambda w: (KIND_ORDER.get(w.kind, 99), -w.memory))

    def _classify(
        self, process: psutil.Process, blobs: dict[str, dict], loaded: list[dict], gpu: dict
    ) -> tuple[str, str, str] | None:
        name = process.info.get("name") or ""
        cmdline = process.info.get("cmdline") or []
        joined = " ".join(cmdline)
        lowered = f"{name} {_program(name, cmdline)}".lower()

        if process.pid == self.backend_pid or self._is_nova_backend(process, name, joined):
            return "nova", "nova backend", "whisper wake + question, piper voice"
        if name in PYTHON_NAMES and "/modules/" in joined and joined.endswith("main.py"):
            module = Path(cmdline[-1]).parent.name if cmdline else "?"
            return "module", f"module {module}", "nova module call"
        if "api.main" in joined or "nova-api" in joined:
            return "nova", "nova api", "rest + websocket"
        if "nova-debug" in joined or "debug.main" in joined:
            return None
        if any(runner in name for runner in LLM_RUNNERS) or " runner " in f" {joined} ":
            return "llm", *self._llm_identity(joined, blobs, loaded)
        if name == "ollama" and "serve" in cmdline:
            return "server", "ollama serve", "model server, loads runners on demand"
        if any(keyword in lowered for keyword in AI_KEYWORDS):
            return "ai", name, _short(joined)
        if name in PYTHON_NAMES and gpu.get(process.pid, 0.0) > 0:
            return "ai", "python on gpu", _short(joined)
        return None

    def _is_nova_backend(self, process: psutil.Process, name: str, joined: str) -> bool:
        if name not in PYTHON_NAMES and "python" not in name.lower():
            return False
        if "apps/backend" in joined:
            return True
        if not joined.endswith("main.py"):
            return False
        try:
            return process.cwd().endswith("apps/backend")
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return False

    def _llm_vram(self, blobs: dict[str, dict], cmdline: list[str]) -> int | None:
        flagged = MODEL_FLAG.search(" ".join(cmdline))
        model = blobs.get(Path(flagged.group(1)).name) if flagged else None
        return model.get("size_vram") if model else None

    def _llm_identity(self, joined: str, blobs: dict[str, dict], loaded: list[dict]) -> tuple[str, str]:
        flagged = MODEL_FLAG.search(joined)
        blob = Path(flagged.group(1)).name if flagged else ""
        model = blobs.get(blob)
        if model is None and len(loaded) == 1 and not blobs:
            model = loaded[0]
        if model is None:
            return (f"unknown model ({blob[:19] or '?'})", "llm runner")
        details = model.get("details") or {}
        total = model.get("size") or 0
        on_gpu = model.get("size_vram") or 0
        share = f"{on_gpu / total * 100:.0f}% on gpu" if total else ""
        parts = [
            details.get("parameter_size", ""),
            details.get("quantization_level", ""),
            f"ctx {model['context_length']}" if model.get("context_length") else "",
            share,
        ]
        return model.get("name", "?"), " · ".join(part for part in parts if part)

    def _blobs(self, host: str, loaded: list[dict]) -> dict[str, dict]:
        mapped: dict[str, dict] = {}
        for model in loaded:
            name = model.get("name")
            if not name:
                continue
            if name not in self._blob_names:
                try:
                    self._blob_names[name] = probes.ollama_blob(host, name)
                except probes.OllamaError:
                    continue
            mapped[self._blob_names[name]] = model
        return mapped


KIND_ORDER = {"llm": 0, "nova": 1, "module": 2, "ai": 3, "server": 4}


def _program(name: str, cmdline: list[str]) -> str:
    if not cmdline:
        return name
    if name not in PYTHON_NAMES and "python" not in name.lower():
        return cmdline[0]
    arguments = []
    skip = False
    for argument in cmdline[1:]:
        if skip:
            skip = False
            continue
        if argument == "-c":
            skip = True
            continue
        arguments.append(argument)
    return " ".join([cmdline[0], *arguments])


def _short(command: str, width: int = 60) -> str:
    return command if len(command) <= width else command[: width - 1] + "…"
