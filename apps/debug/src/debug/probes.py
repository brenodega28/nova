from __future__ import annotations

import ctypes
import ctypes.util
import json
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

import psutil

PROBE_TIMEOUT = 3.0
RUSAGE_INFO_V2 = 2
PHYS_FOOTPRINT_OFFSET = 16 + 7 * 8
IOREG_NUMBER = r'"{key}"=(\d+)'


@dataclass
class Gpu:
    name: str
    utilization: float | None
    memory_used: int | None
    memory_total: int | None


@dataclass
class ProcessSample:
    pid: int
    cpu: float
    memory: int
    threads: int


def system() -> dict:
    memory = psutil.virtual_memory()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "cores": psutil.cpu_percent(interval=None, percpu=True),
        "memory_used": memory.total - memory.available,
        "memory_total": memory.total,
        "memory_percent": memory.percent,
    }


def gpu() -> Gpu | None:
    if shutil.which("nvidia-smi"):
        return _nvidia()
    if sys.platform == "darwin":
        return _apple()
    return None


def _nvidia() -> Gpu | None:
    try:
        output = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    first = output.strip().splitlines()[0] if output.strip() else ""
    parts = [part.strip() for part in first.split(",")]
    if len(parts) != 4:
        return None
    name, utilization, used, total = parts
    mebibyte = 1024 * 1024
    return Gpu(name, float(utilization), int(used) * mebibyte, int(total) * mebibyte)


def _apple() -> Gpu | None:
    try:
        output = subprocess.run(
            ["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    name = re.search(r'"model" = "([^"]+)"', output)
    utilization = _ioreg_number(output, "Device Utilization %")
    in_use = _ioreg_number(output, "In use system memory")
    return Gpu(
        name.group(1) if name else "Apple GPU",
        float(utilization) if utilization is not None else None,
        in_use,
        psutil.virtual_memory().total,
    )


def _ioreg_number(output: str, key: str) -> int | None:
    found = re.search(IOREG_NUMBER.format(key=re.escape(key)), output)
    return int(found.group(1)) if found else None


class Watcher:
    def __init__(self):
        self._processes: dict[int, psutil.Process] = {}

    def sample(self, pid: int) -> ProcessSample | None:
        process = self._processes.get(pid)
        try:
            if process is None:
                process = psutil.Process(pid)
                process.cpu_percent(interval=None)
                self._processes[pid] = process
            with process.oneshot():
                return ProcessSample(
                    pid=pid,
                    cpu=process.cpu_percent(interval=None),
                    memory=max(footprint(pid) or 0, process.memory_info().rss),
                    threads=process.num_threads(),
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            self._processes.pop(pid, None)
            return None

    def forget_except(self, pids: set[int]) -> None:
        for pid in set(self._processes) - pids:
            self._processes.pop(pid, None)


def _libproc():
    if sys.platform != "darwin":
        return None
    path = ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib"
    try:
        return ctypes.CDLL(path)
    except OSError:
        return None


_LIBPROC = _libproc()


def footprint(pid: int) -> int | None:
    if _LIBPROC is None:
        return None
    buffer = ctypes.create_string_buffer(512)
    if _LIBPROC.proc_pid_rusage(ctypes.c_int(pid), ctypes.c_int(RUSAGE_INFO_V2), buffer) != 0:
        return None
    return int.from_bytes(
        buffer.raw[PHYS_FOOTPRINT_OFFSET : PHYS_FOOTPRINT_OFFSET + 8], sys.byteorder
    )


class OllamaError(RuntimeError):
    pass


def ollama_loaded(host: str) -> list[dict]:
    return _ollama(host, "/api/ps").get("models") or []


def ollama_blob(host: str, name: str) -> str:
    modelfile = _ollama(host, "/api/show", {"model": name}).get("modelfile") or ""
    for line in modelfile.splitlines():
        if line.startswith("FROM ") and "sha256" in line:
            return line.removeprefix("FROM ").strip().rsplit("/", 1)[-1]
    raise OllamaError(f"no weights found for {name}")


def ollama_unload(host: str, name: str) -> None:
    _ollama(host, "/api/generate", {"model": name, "keep_alive": 0})


def _ollama(host: str, path: str, payload: dict | None = None) -> dict:
    request = urllib.request.Request(
        f"{host.rstrip('/')}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise OllamaError(f"ollama refused {path}: {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise OllamaError(f"cannot reach ollama at {host}") from exc
