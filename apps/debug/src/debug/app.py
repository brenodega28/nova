from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import psutil
from rich.markup import escape
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.theme import Theme
from textual.widgets import DataTable, Footer, Header, RichLog, Sparkline, Static

from debug import probes, workloads
from debug.backend import Backend, Refused, Unreachable

GIGABYTE = 1024**3
HISTORY = 120
BAR_WIDTH = 20
CORES = psutil.cpu_count() or 1

BLUE = Theme(
    name="nova-blue",
    primary="#0178d4",
    secondary="#004578",
    accent="#4aa8ff",
    warning="#e0a84a",
    error="#ba3c5b",
    success="#4ebf71",
    foreground="#e0e0e0",
    dark=True,
)

KIND_STYLE = {
    "llm": "[magenta]llm[/]",
    "nova": "[cyan]nova[/]",
    "module": "[yellow]module[/]",
    "ai": "[green]ai[/]",
    "server": "[dim]server[/]",
}

QUIET_EVENTS = {"answer_chunk", "ping", "scanned", "hello"}


@dataclass
class Config:
    host: str
    port: int
    token: str
    ollama_host: str
    interval: float
    memory_limit: float
    auto_reload: bool
    over_limit_samples: int
    cooldown: float


def size(value: float | None) -> str:
    if value is None:
        return "—"
    for unit, scale in (("GB", GIGABYTE), ("MB", 1024**2), ("KB", 1024)):
        if value >= scale:
            return f"{value / scale:.1f} {unit}"
    return f"{int(value)} B"


def count(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1e9:
        return f"{value / 1e9:.2f}B"
    if value >= 1e6:
        return f"{value / 1e6:.0f}M"
    return str(value)


def bar(percent: float | None, width: int = BAR_WIDTH) -> str:
    if percent is None:
        return "[dim]" + "·" * width + "[/]"
    filled = round(max(0.0, min(percent, 100.0)) / 100 * width)
    colour = "green" if percent < 60 else "yellow" if percent < 85 else "red"
    return f"[{colour}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


def duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def expires_in(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp() - time.time()
    except ValueError:
        return None


class Panel(Static):
    def show(self, lines: list[str]) -> None:
        self.update(Text.from_markup("\n".join(lines)))


class DebugApp(App):
    TITLE = "nova debug"
    CSS = """
    Screen { background: $surface; }
    #top { height: auto; }
    Panel {
        width: 1fr;
        height: auto;
        min-height: 9;
        border: round $primary;
        padding: 0 1;
    }
    #trends { height: 4; }
    .trend { width: 1fr; height: 4; padding: 0 1; }
    .trend-label { height: 1; color: $text-muted; }
    Sparkline { height: 3; }
    #workloads { height: auto; max-height: 14; border: round $primary; }
    #models { height: auto; max-height: 8; border: round $primary; }
    #events { height: 1fr; border: round $primary; }
    """
    BINDINGS = [
        Binding("r", "restart", "restart listener"),
        Binding("R", "reload", "reload process"),
        Binding("u", "unload", "unload LLM"),
        Binding("a", "toggle_auto", "auto reload"),
        Binding("plus,equals_sign", "limit(1)", "limit +1GB"),
        Binding("minus", "limit(-1)", "limit -1GB"),
        Binding("c", "clear", "clear log"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config
        self.backend = Backend(config.host, config.port, config.token, self._on_backend_event)
        self.scanner = workloads.Scanner()
        self.workloads: list[workloads.Workload] = []
        self.diagnostics: dict | None = None
        self.picture: dict | None = None
        self.loaded: list[dict] = []
        self.ollama_error: str | None = None
        self.cpu_history: deque[float] = deque([0.0] * HISTORY, maxlen=HISTORY)
        self.gpu_history: deque[float] = deque([0.0] * HISTORY, maxlen=HISTORY)
        self.memory_history: deque[float] = deque([0.0] * HISTORY, maxlen=HISTORY)
        self.over_limit = 0
        self.last_action_at = 0.0
        self._sampling = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="top"):
            yield Panel(id="backend")
            yield Panel(id="system")
            yield Panel(id="ollama")
        with Horizontal(id="trends"):
            for label, name, history in (
                ("cpu %", "cpu", self.cpu_history),
                ("gpu %", "gpu", self.gpu_history),
                ("ai memory, GB", "memory", self.memory_history),
            ):
                with Vertical(classes="trend"):
                    yield Static(label, classes="trend-label")
                    yield Sparkline(list(history), id=f"{name}-spark")
        yield DataTable(id="workloads", cursor_type="none", zebra_stripes=True)
        yield DataTable(id="models", cursor_type="none", zebra_stripes=True)
        yield RichLog(id="events", markup=True, wrap=True, max_lines=1000)
        yield Footer()

    def on_mount(self) -> None:
        self.register_theme(BLUE)
        self.theme = BLUE.name
        self.query_one("#backend", Panel).border_title = "backend"
        self.query_one("#system", Panel).border_title = "system"
        self.query_one("#ollama", Panel).border_title = "ollama"
        self.query_one("#workloads", DataTable).border_title = "ai workloads · every process, backend or not"
        self.query_one("#models", DataTable).border_title = "models inside the backend"
        self.query_one("#workloads", DataTable).add_columns(
            "kind", "name", "pid", "cpu %", "gpu %", "memory", "gpu mem", "threads", "detail"
        )
        self.query_one("#events", RichLog).border_title = "events"
        self.query_one("#models", DataTable).add_columns(
            "role", "engine", "model", "device", "params", "memory", "state"
        )
        probes.system()
        self.backend.start()
        self.log_line(
            f"watching {self.config.host}:{self.config.port} · "
            f"auto reload {'on' if self.config.auto_reload else 'off'} "
            f"above {self.config.memory_limit:g} GB",
            "dim",
        )
        self.set_interval(self.config.interval, self.sample)
        self.call_later(self.sample)

    async def on_unmount(self) -> None:
        await self.backend.stop()

    def log_line(self, text: str, style: str = "") -> None:
        stamp = time.strftime("%H:%M:%S")
        body = f"[{style}]{text}[/]" if style else text
        self.query_one("#events", RichLog).write(f"[dim]{stamp}[/] {body}")

    def _on_backend_event(self, frame: dict) -> None:
        event = frame.get("event", "")
        data = frame.get("data") or {}
        if event in QUIET_EVENTS:
            return
        described = {
            "connected": (f"connected to {data.get('host')}:{data.get('port')}", "green"),
            "disconnected": (f"backend gone — {data.get('reason', '')}", "red"),
            "starting": ("listener starting", "cyan"),
            "setup": (f"setup · {data.get('text', '')}", "cyan"),
            "ready": (
                f"ready · noise floor {data.get('noise_floor', 0):.4f} gate {data.get('gate', 0):.4f}",
                "green",
            ),
            "wake": (f"wake #{data.get('count')} ({data.get('latency', 0) * 1000:.0f} ms)", "magenta"),
            "question": (f"you · {data.get('text', '')}", ""),
            "thinking": ("thinking…", "dim"),
            "answer": (f"answer ({data.get('seconds', 0):.1f}s) · {data.get('text', '')}", "green"),
            "module": (
                f"module {data.get('module')}.{data.get('tool')} ({data.get('seconds', 0):.1f}s)",
                "yellow",
            ),
            "timeout": ("no question — back to listening", "dim"),
            "error": (f"error · {data.get('message', '')}", "red"),
            "broken": (f"broken · {data.get('message', '')}", "bold red"),
            "settings": ("settings changed", "yellow"),
            "summary": (f"summary · {data.get('wakes')} wakes", "dim"),
        }
        text, style = described.get(event, (f"{event} {data}", "dim"))
        self.log_line(escape(text), style)

    async def sample(self) -> None:
        if self._sampling:
            return
        self._sampling = True
        try:
            await self._sample()
        finally:
            self._sampling = False

    async def _sample(self) -> None:
        system, gpu = await asyncio.gather(
            asyncio.to_thread(probes.system), asyncio.to_thread(probes.gpu)
        )
        await self._ask_backend()
        await self._ask_ollama()
        self.scanner.backend_pid = self.diagnostics["pid"] if self.diagnostics else None
        self.workloads = await asyncio.to_thread(self.scanner.scan, self.ollama_host, self.loaded)
        process = self._backend_workload()

        self.cpu_history.append(system["cpu"])
        self.gpu_history.append((gpu.utilization or 0.0) if gpu else 0.0)
        self.memory_history.append(sum(w.memory for w in self.workloads) / GIGABYTE)
        self.query_one("#cpu-spark", Sparkline).data = list(self.cpu_history)
        self.query_one("#gpu-spark", Sparkline).data = list(self.gpu_history)
        self.query_one("#memory-spark", Sparkline).data = list(self.memory_history)

        self._draw_backend(process)
        self._draw_system(system, gpu)
        self._draw_ollama()
        self._draw_workloads()
        self._draw_models()
        await self._watch_memory(process)

    async def _ask_backend(self) -> None:
        if not self.backend.connected:
            self.diagnostics = None
            self.picture = None
            return
        try:
            self.diagnostics, self.picture = await asyncio.gather(
                self.backend.command("diagnostics"), self.backend.command("state")
            )
        except Refused as exc:
            self.diagnostics = None
            self.log_line(escape(f"backend refused diagnostics: {exc}"), "red")
        except Unreachable:
            self.diagnostics = None

    def _backend_workload(self) -> workloads.Workload | None:
        return next((w for w in self.workloads if w.name == "nova backend"), None)

    @property
    def llm(self) -> dict | None:
        if self.diagnostics is None:
            return None
        return next((m for m in self.diagnostics["models"] if m["role"] == "llm"), None)

    @property
    def ollama_host(self) -> str:
        llm = self.llm
        return llm["host"] if llm and llm.get("host") else self.config.ollama_host

    async def _ask_ollama(self) -> None:
        try:
            self.loaded = await asyncio.to_thread(probes.ollama_loaded, self.ollama_host)
            self.ollama_error = None
        except probes.OllamaError as exc:
            self.loaded = []
            self.ollama_error = str(exc)

    def _draw_backend(self, process: workloads.Workload | None) -> None:
        panel = self.query_one("#backend", Panel)
        if not self.backend.connected:
            reason = escape(self.backend.last_error or "not started")
            panel.show(
                [
                    f"[red]○ not connected[/] {self.config.host}:{self.config.port}",
                    f"[dim]{reason}[/]",
                    "",
                    "[dim]start it with[/] uv run main.py [dim]in apps/backend[/]",
                ]
            )
            return
        diagnostics = self.diagnostics or {}
        picture = self.picture or {}
        activity = picture.get("activity", "?")
        colour = {"broken": "red", "loading": "yellow", "idle": "green"}.get(activity, "cyan")
        lines = [
            f"[green]● connected[/] {self.config.host}:{self.config.port}",
            f"activity [{colour}]{activity}[/]   generation {diagnostics.get('generation', '—')}",
            f"pid {diagnostics.get('pid', '—')}   uptime {duration(picture.get('uptime_seconds'))}"
            f"   wakes {picture.get('wakes', 0)}",
        ]
        if process is not None:
            limit = self.config.memory_limit * GIGABYTE
            lines += [
                f"cpu {bar(process.cpu / CORES)} {process.cpu:5.1f}%",
                f"mem {bar(process.memory / limit * 100)} {size(process.memory)}"
                f" / {self.config.memory_limit:g} GB",
                f"threads {process.threads}",
            ]
        torch = diagnostics.get("torch") or {}
        if torch.get("imported"):
            lines.append(
                f"torch {torch.get('version')} on [bold]{torch.get('device')}[/]"
                f"  alloc {size(torch.get('allocated'))}  driver {size(torch.get('reserved'))}"
            )
        if picture.get("last_error"):
            lines.append(f"[red]last error[/] {escape(picture['last_error'])}")
        state = "[green]on[/]" if self.config.auto_reload else "[dim]off[/]"
        lines.append(
            f"auto reload {state}  over limit {self.over_limit}/{self.config.over_limit_samples}"
        )
        panel.show(lines)

    def _draw_system(self, system: dict, gpu: probes.Gpu | None) -> None:
        lines = [
            f"cpu {bar(system['cpu'])} {system['cpu']:5.1f}%  ({len(system['cores'])} cores)",
            f"ram {bar(system['memory_percent'])} {size(system['memory_used'])}"
            f" / {size(system['memory_total'])}",
        ]
        if gpu is None:
            lines.append("gpu [dim]not detected[/]")
        else:
            lines.append(f"gpu {escape(gpu.name)}")
            lines.append(
                f"    {bar(gpu.utilization)} "
                + (f"{gpu.utilization:5.1f}%" if gpu.utilization is not None else "n/a")
            )
            if gpu.memory_used is not None and gpu.memory_total:
                lines.append(
                    f"vram {bar(gpu.memory_used / gpu.memory_total * 100)} "
                    f"{size(gpu.memory_used)} / {size(gpu.memory_total)}"
                )
        self.query_one("#system", Panel).show(lines)

    def _draw_ollama(self) -> None:
        lines = [escape(self.ollama_host)]
        if self.ollama_error:
            lines.append(f"[red]○ {escape(self.ollama_error)}[/]")
        else:
            lines.append("[green]● up[/]")
        ollama = [w for w in self.workloads if w.kind in ("llm", "server")]
        if ollama:
            lines.append(
                f"cpu {sum(w.cpu for w in ollama):5.1f}%   mem {size(sum(w.memory for w in ollama))}"
                f"   ({len(ollama)} processes)"
            )
        if not self.loaded:
            lines.append("[dim]no model loaded[/]")
        for model in self.loaded:
            total = model.get("size") or 0
            on_gpu = model.get("size_vram") or 0
            share = f"{on_gpu / total * 100:.0f}% gpu" if total else "—"
            lines.append(
                f"[bold]{escape(model.get('name', '?'))}[/] {size(total)} ({share})"
                f"  expires {duration(expires_in(model.get('expires_at')))}"
            )
        self.query_one("#ollama", Panel).show(lines)

    def _draw_workloads(self) -> None:
        table = self.query_one("#workloads", DataTable)
        table.clear()
        if not self.workloads:
            table.add_row("—", "[dim]no ai processes running[/]", "", "", "", "", "", "", "")
            return
        diagnostics = self.diagnostics or {}
        for workload in self.workloads:
            detail = workload.detail
            if workload.name == "nova backend" and diagnostics:
                detail = " · ".join(
                    f"{m['role']} {m['name']}"
                    for m in diagnostics.get("models", [])
                    if m["role"] != "llm"
                )
            table.add_row(
                KIND_STYLE.get(workload.kind, workload.kind),
                escape(workload.name),
                str(workload.pid),
                f"{workload.cpu:.1f}",
                f"{workload.gpu:.1f}" if workload.gpu is not None else "—",
                size(workload.memory),
                size(workload.gpu_memory) if workload.gpu_memory is not None else "—",
                str(workload.threads),
                escape(detail),
            )
        gpu_shares = [w.gpu for w in self.workloads if w.gpu is not None]
        gpu_memory = [w.gpu_memory for w in self.workloads if w.gpu_memory is not None]
        table.add_row(
            "[bold]total[/]",
            f"[bold]{len(self.workloads)} processes[/]",
            "",
            f"[bold]{sum(w.cpu for w in self.workloads):.1f}[/]",
            f"[bold]{sum(gpu_shares):.1f}[/]" if gpu_shares else "—",
            f"[bold]{size(sum(w.memory for w in self.workloads))}[/]",
            f"[bold]{size(sum(gpu_memory))}[/]" if gpu_memory else "—",
            "",
            "",
        )

    def _draw_models(self) -> None:
        table = self.query_one("#models", DataTable)
        table.clear()
        models = (self.diagnostics or {}).get("models") or []
        if not models:
            table.add_row("—", "", "[dim]backend not connected[/]", "", "", "", "")
            return
        for model in models:
            if model["role"] == "llm":
                table.add_row(*self._llm_row(model))
                continue
            state = "[green]loaded[/]" if model.get("loaded") else "[dim]not loaded[/]"
            if model.get("speaking"):
                state = "[cyan]speaking[/]"
            table.add_row(
                model["role"],
                model["engine"],
                model["name"],
                model.get("device") or "—",
                count(model.get("parameters")),
                size(model.get("bytes")),
                state,
            )

    def _llm_row(self, model: dict) -> tuple:
        loaded = next((m for m in self.loaded if m.get("name") == model["name"]), None)
        if loaded is None:
            state = "[red]unreachable[/]" if self.ollama_error else "[dim]not loaded[/]"
            return ("llm", "ollama", model["name"], "—", "—", "—", state)
        details = loaded.get("details") or {}
        total = loaded.get("size") or 0
        on_gpu = loaded.get("size_vram") or 0
        device = "gpu" if on_gpu >= total else f"{on_gpu / total * 100:.0f}% gpu" if total else "cpu"
        quantization = details.get("quantization_level")
        return (
            "llm",
            "ollama",
            model["name"] + (f" ({quantization})" if quantization else ""),
            device,
            details.get("parameter_size", "—"),
            size(total),
            "[green]loaded[/]",
        )

    async def _watch_memory(self, process: workloads.Workload | None) -> None:
        if process is None or not self.backend.connected:
            self.over_limit = 0
            return
        if process.memory <= self.config.memory_limit * GIGABYTE:
            self.over_limit = 0
            return
        self.over_limit += 1
        if not self.config.auto_reload:
            return
        if self.over_limit < self.config.over_limit_samples:
            return
        if time.time() - self.last_action_at < self.config.cooldown:
            return
        self.log_line(
            f"backend at {size(process.memory)}, over {self.config.memory_limit:g} GB "
            f"for {self.over_limit} samples — reloading",
            "bold yellow",
        )
        self.over_limit = 0
        await self._command("reload", "reload requested")

    async def _command(self, name: str, done: str) -> None:
        try:
            await self.backend.command(name)
        except (Refused, Unreachable) as exc:
            self.log_line(escape(f"{name} failed: {exc}"), "red")
            return
        self.last_action_at = time.time()
        self.log_line(done, "yellow")

    async def action_restart(self) -> None:
        await self._command("restart", "restart requested — rebuilding the listener")

    async def action_reload(self) -> None:
        await self._command("reload", "reload requested — replacing the process")

    async def action_unload(self) -> None:
        names = [m.get("name") for m in self.loaded if m.get("name")]
        if not names:
            self.log_line("no ollama model loaded", "dim")
            return
        for name in names:
            try:
                await asyncio.to_thread(probes.ollama_unload, self.ollama_host, name)
            except probes.OllamaError as exc:
                self.log_line(escape(f"unload {name} failed: {exc}"), "red")
                continue
            self.log_line(escape(f"unloaded {name} — it loads again on the next question"), "yellow")
        await self.sample()

    def action_toggle_auto(self) -> None:
        self.config.auto_reload = not self.config.auto_reload
        self.log_line(f"auto reload {'on' if self.config.auto_reload else 'off'}", "yellow")

    def action_limit(self, step: int) -> None:
        self.config.memory_limit = max(1.0, self.config.memory_limit + step)
        self.log_line(f"memory limit {self.config.memory_limit:g} GB", "yellow")

    def action_clear(self) -> None:
        self.query_one("#events", RichLog).clear()

