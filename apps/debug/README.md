# nova-debug

A terminal view into a running backend: which models are loaded and where,
how much CPU, GPU and memory she is using, what she is hearing and saying, and
buttons to restart her when memory runs away.

```
apps/backend          mic, Whisper, Piper, Ollama
      │  127.0.0.1:8765, newline-delimited JSON
apps/debug            this                                   ← you are here
      │  psutil, ioreg / nvidia-smi, Ollama /api/ps
```

It speaks to the same control port `apps/api` does, and asks it for
`diagnostics` and `state` once a second. Everything else — CPU, memory, GPU,
Ollama, every AI process on the machine — it reads for itself, so all of that
keeps working while the backend is down.

## Running it

```sh
uv run nova-debug
```

Start the backend first, or don't — it waits and reconnects.

## What it shows

| panel | shows |
| --- | --- |
| backend | connection, activity, generation, pid, uptime, CPU, memory against the limit, threads, torch device and allocations |
| system | total CPU, RAM, GPU utilization and GPU memory |
| ollama | whether it is up, its process CPU and memory, every model it has resident |
| sparklines | the last two minutes of CPU, GPU and total AI memory |
| ai workloads | every AI process running, backend or not — CPU, GPU, memory, GPU memory and threads each, and a total |
| models inside the backend | wake and question Whisper models, the Piper voice and the LLM, as the backend reports them |
| events | the live stream from the control port — wakes, questions, answers, modules, errors |

## AI workloads

Found by scanning the process table every sample, so nothing has to be running
for it to work and nothing has to report in:

| kind | what is recognised |
| --- | --- |
| `llm` | an Ollama runner (`llama-server`), named after the model it is serving by matching its weights file against `/api/show` |
| `nova` | the backend — a Python `main.py` in `apps/backend`, or the pid the control port reports — and `nova-api` |
| `module` | a Nova module call in flight |
| `ai` | anything whose program or script names whisper, llama, mlx, piper, vosk and the like, and any Python process using the GPU |
| `server` | `ollama serve` itself |

GPU % is per process. On macOS it is the GPU time each process spent since the
last sample, read from `ioreg`; with an NVIDIA card it is `nvidia-smi pmon`, and
GPU memory comes from `nvidia-smi` too. For an LLM, GPU memory is what Ollama
reports holding on the GPU.

The Whisper models share the backend's process, so the OS cannot tell them
apart — their individual sizes come from the backend's `diagnostics`, in the
table below, while it is connected.

Memory is the process's physical footprint on macOS — the number
Activity Monitor shows, which counts Metal allocations that RSS does not — or
RSS, whichever is larger. RSS wins for an LLM, whose weights are memory-mapped
from disk and so left out of the footprint.

GPU comes from `nvidia-smi` when it is installed and from `ioreg` on macOS,
neither of which needs sudo.

## Keys

| key | does |
| --- | --- |
| `r` | restart the listener — rebuild it in the same process |
| `R` | reload the process — `execv`, frees everything |
| `u` | unload every model Ollama has resident; it loads again on the next question |
| `a` | toggle auto reload |
| `+` / `-` | move the memory limit by 1 GB |
| `c` | clear the event log |
| `q` | quit |

## Auto reload

When the backend's memory stays above the limit for
`NOVA_DEBUG_OVER_LIMIT_SAMPLES` samples in a row, it sends `reload` — a full
process replacement, because a `restart` keeps the interpreter and whatever
torch has cached with it. It then waits `NOVA_DEBUG_COOLDOWN` seconds before it
will do so again, so a limit set below what a cold start needs cannot loop.

## Configuration

| variable | default | meaning |
| --- | --- | --- |
| `NOVA_BACKEND_HOST` | `127.0.0.1` | where the backend's control port is |
| `NOVA_BACKEND_PORT` | `8765` | that port |
| `NOVA_CONTROL_TOKEN` | none | token the backend wants, if it wants one |
| `NOVA_OLLAMA_HOST` | `http://localhost:11434` | used until the backend reports its own |
| `NOVA_DEBUG_INTERVAL` | `1.0` | seconds between samples |
| `NOVA_DEBUG_MEMORY_LIMIT_GB` | `12` | backend memory that counts as too much |
| `NOVA_DEBUG_AUTO_RELOAD` | on | reload automatically above the limit |
| `NOVA_DEBUG_OVER_LIMIT_SAMPLES` | `5` | consecutive samples over the limit before acting |
| `NOVA_DEBUG_COOLDOWN` | `180` | seconds between automatic reloads |
