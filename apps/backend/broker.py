"""Finding installed modules and calling them: the host side of the contract.

    from broker import Registry, Runner

    registry = Registry()
    module = registry.match("what's the weather in Lisbon")
    answer = Runner().call(module, "current_conditions", {"location": "Lisbon"})
    print(answer["speech"])

Two halves, deliberately separate.

:class:`Registry` reads manifests and nothing else. Finding out what a module is
called, what it answers to and what it wants reached never runs a line of its
code, because deciding whether to trust something cannot require executing it
first. That is the property that makes an install-time permission sheet possible.

:class:`Runner` is the only thing here that executes anything. It spawns a fresh
process per call — around 25 ms, so state never carries between calls and nothing
stays resident — hands the request over on stdin, and reads one JSON envelope
back. Timeouts kill. Output is capped. The envelope is re-checked on the way in,
because a module's answer is as untrusted as its arguments were — including the
case of a module claiming success with nothing to say, which would otherwise reach
the speaker as silence and read as a hang.

:class:`Sandbox` is the seam isolation slots into. ``Direct`` is what ships:
no isolation, appropriate for modules you wrote. The shape is here now because a
contract that assumed ambient authority cannot be given a boundary later without
breaking every module written against it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sdk import ENTRY_NAME, MANIFEST_NAME, SUPPORTED_MANIFEST
from text import normalize

MODULES_DIR = Path.home() / ".nova" / "apps"
MAX_OUTPUT = 256 * 1024
MAX_SPEECH = 1000
MAX_DETAIL = 400
DEFAULT_TIMEOUT = 10.0


class Broken(RuntimeError):
    """A module on disk is not one we can use."""


@dataclass(frozen=True)
class Grants:
    """What a module asked for, as the manifest declared it."""

    egress: tuple[str, ...] = ()
    read: tuple[str, ...] = ()
    write: tuple[str, ...] = ()
    secrets: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT
    consequence: str = "read-only"
    confirm: bool = False

    @classmethod
    def read_from(cls, declared: dict) -> "Grants":
        return cls(
            egress=tuple(declared.get("egress") or ()),
            read=tuple(declared.get("read") or ()),
            write=tuple(declared.get("write") or ()),
            secrets=tuple(declared.get("secrets") or ()),
            timeout=float(declared.get("timeout_seconds", DEFAULT_TIMEOUT)),
            consequence=str(declared.get("consequence", "read-only")),
            confirm=bool(declared.get("confirm", False)),
        )

    def spoken_summary(self) -> str:
        """What to read out, or show, before letting a module be installed."""
        wants = []
        if self.egress:
            wants.append(f"reach {', '.join(self.egress)}")
        if self.read:
            wants.append(f"read {', '.join(self.read)}")
        if self.write:
            wants.append(f"write {', '.join(self.write)}")
        if self.secrets:
            wants.append(f"use {', '.join(self.secrets)}")
        return "; ".join(wants) or "nothing outside itself"


@dataclass(frozen=True)
class Installed:
    """A module found on disk, as far as its manifest describes it."""

    name: str
    directory: Path
    version: str = "0.0.0"
    summary: str = ""
    phrases: tuple[str, ...] = ()
    grants: Grants = field(default_factory=Grants)
    settings: dict = field(default_factory=dict)

    @property
    def entry(self) -> Path:
        return self.directory / ENTRY_NAME


def read_manifest(directory: Path) -> Installed:
    """Describe a module from its manifest alone, running none of its code."""
    manifest_file = directory / MANIFEST_NAME
    if not manifest_file.is_file():
        raise Broken(f"{directory.name} has no {MANIFEST_NAME}")
    if not (directory / ENTRY_NAME).is_file():
        raise Broken(f"{directory.name} has no {ENTRY_NAME}")

    try:
        with manifest_file.open("rb") as handle:
            manifest = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise Broken(f"{directory.name}: {MANIFEST_NAME} will not parse ({exc})") from exc

    version = manifest.get("manifest_version")
    if version != SUPPORTED_MANIFEST:
        raise Broken(
            f"{directory.name}: manifest_version {version!r}, "
            f"this host speaks {SUPPORTED_MANIFEST}"
        )

    described = manifest.get("module") or {}
    name = described.get("name") or directory.name
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
        raise Broken(f"{directory.name}: {name!r} is not a usable module name")

    return Installed(
        name=name,
        directory=directory,
        version=str(described.get("version", "0.0.0")),
        summary=str(described.get("summary", "")),
        phrases=tuple(str(p).lower() for p in (described.get("phrases") or ())),
        grants=Grants.read_from(manifest.get("grants") or {}),
        settings=manifest.get("settings") or {},
    )


class Registry:
    """Which modules are installed, and which one a sentence is asking for."""

    def __init__(self, root: Path = MODULES_DIR):
        self.root = root
        self.modules: dict[str, Installed] = {}
        self.broken: list[str] = []
        self.discover()

    def discover(self) -> dict[str, Installed]:
        """Read every manifest under the root. Executes nothing."""
        self.modules = {}
        self.broken = []
        if not self.root.is_dir():
            return self.modules
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or directory.name.startswith((".", "_")):
                continue
            try:
                found = read_manifest(directory)
            except Broken as exc:
                self.broken.append(str(exc))
                continue
            if found.name in self.modules:
                self.broken.append(f"{directory.name}: {found.name!r} is already taken")
                continue
            self.modules[found.name] = found
        return self.modules

    def get(self, name: str) -> Installed | None:
        return self.modules.get(name)

    def match(self, said: str) -> Installed | None:
        """Pick a module from what was said, on declared phrases alone.

        Deterministic on purpose: routing a sentence to a module is not a job
        worth waking a language model for, and a model that picks the tool is a
        model whose input decides what runs. The longest phrase wins, so a module
        claiming "weather forecast" beats one claiming "weather".

        A phrase matches its own continuations, so "rain" covers raining, rains
        and rainy. Declaring the stem is the point: a manifest that says "raining"
        and a speaker who says "will it rain" would otherwise miss each other, and
        a miss here does not fail safely — it falls through to a model with no
        weather data and every willingness to describe some.
        """
        heard = normalize(said)
        if not heard:
            return None
        best: Installed | None = None
        best_length = 0
        for module in self.modules.values():
            for phrase in module.phrases:
                spoken = normalize(phrase)
                if not spoken or len(spoken) <= best_length:
                    continue
                if re.search(rf"\b{re.escape(spoken)}\w*\b", heard):
                    best, best_length = module, len(spoken)
        return best


class Sandbox(Protocol):
    """How a module's command is wrapped before it runs."""

    def wrap(self, module: Installed, command: list[str]) -> list[str]: ...


class Direct:
    """No isolation: the module runs as a plain child process.

    Right for modules you wrote and read. Everything a third-party module would
    need holding back from — the filesystem, the network, the environment — it
    still has here, which is what the seam above exists to change.
    """

    def wrap(self, module: Installed, command: list[str]) -> list[str]:
        return command


@dataclass
class Call:
    """One invocation, as it happened."""

    module: str
    tool: str
    args: dict
    ok: bool
    speech: str
    data: dict
    error: str | None
    seconds: float


class Runner:
    """Runs one tool, in its own process, and brings back one answer."""

    def __init__(
        self,
        sandbox: Sandbox | None = None,
        python: str = sys.executable,
    ):
        self.sandbox = sandbox or Direct()
        self.python = python
        self._offered: dict[str, list[dict]] = {}

    def _spawn(self, module: Installed, argv: list[str], stdin: str, timeout: float):
        command = self.sandbox.wrap(module, [self.python, str(module.entry), *argv])
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=module.directory,
        )

    def schemas(self, module: Installed) -> list[dict]:
        """Ask a module what tools it offers, once.

        Unlike reading the manifest, this runs the module — so it belongs inside
        whatever sandbox is in force, and the answer is held for the life of the
        process rather than asked again every time somebody speaks.
        """
        if module.name in self._offered:
            return self._offered[module.name]
        self._offered[module.name] = self._ask_schemas(module)
        return self._offered[module.name]

    def _ask_schemas(self, module: Installed) -> list[dict]:
        try:
            done = self._spawn(module, ["--schemas"], "", module.grants.timeout)
        except subprocess.TimeoutExpired:
            return []
        if done.returncode != 0:
            return []
        try:
            offered = json.loads(done.stdout[:MAX_OUTPUT])
        except ValueError:
            return []
        return offered if isinstance(offered, list) else []

    def call(self, module: Installed, tool: str, args: dict | None = None) -> Call:
        """Run one tool and bring back an answer that is safe to speak."""
        args = args or {}
        request = json.dumps({"tool": tool, "args": args})
        started = time.monotonic()

        try:
            done = self._spawn(module, [], request, module.grants.timeout)
        except subprocess.TimeoutExpired:
            return self._failed(
                module,
                tool,
                args,
                started,
                f"{module.name} took longer than {module.grants.timeout:g}s",
                f"{module.name} took too long.",
            )
        except OSError as exc:
            return self._failed(
                module,
                tool,
                args,
                started,
                str(exc),
                f"I couldn't start {module.name}.",
            )

        if len(done.stdout) > MAX_OUTPUT:
            return self._failed(
                module,
                tool,
                args,
                started,
                f"{module.name} wrote more than {MAX_OUTPUT} bytes",
                f"{module.name} said far too much.",
            )

        try:
            envelope = json.loads(done.stdout)
        except ValueError:
            lines = (done.stderr or done.stdout or "").strip().splitlines()
            detail = lines[-1][:MAX_DETAIL] if lines else "nothing"
            return self._failed(
                module,
                tool,
                args,
                started,
                f"{module.name} did not answer with JSON: {detail}",
                f"{module.name} gave me nothing I could read.",
            )

        return self._accept(module, tool, args, started, envelope)

    def _accept(
        self, module: Installed, tool: str, args: dict, started: float, envelope: object
    ) -> Call:
        """Take a module's envelope apart without trusting its shape."""
        if not isinstance(envelope, dict):
            return self._failed(
                module,
                tool,
                args,
                started,
                "the answer was not an object",
                f"{module.name} gave me a strange answer.",
            )

        speech = envelope.get("speech")
        speech = speech.strip() if isinstance(speech, str) else ""
        if len(speech) > MAX_SPEECH:
            speech = speech[:MAX_SPEECH].rsplit(" ", 1)[0] + "…"
        data = envelope.get("data")
        error = envelope.get("error")
        ok = bool(envelope.get("ok"))

        if ok and not speech:
            return self._failed(
                module,
                tool,
                args,
                started,
                f"{module.name} answered ok but said nothing",
                f"{module.name} had nothing to say.",
            )

        return self._finish(
            module,
            tool,
            args,
            started,
            ok,
            str(error)[:MAX_DETAIL] if error is not None else None,
            speech,
            data if isinstance(data, dict) else {},
        )

    def _failed(
        self,
        module: Installed,
        tool: str,
        args: dict,
        started: float,
        error: str,
        speech: str,
    ) -> Call:
        return self._finish(module, tool, args, started, False, error, speech, {})

    def _finish(
        self,
        module: Installed,
        tool: str,
        args: dict,
        started: float,
        ok: bool,
        error: str | None,
        speech: str,
        data: dict,
    ) -> Call:
        return Call(
            module=module.name,
            tool=tool,
            args=args,
            ok=ok,
            speech=speech,
            data=data,
            error=error,
            seconds=round(time.monotonic() - started, 3),
        )


def _cli(argv: list[str]) -> int:
    registry = Registry()
    runner = Runner()

    if not argv or argv[0] in ("-h", "--help"):
        print(
            "broker list                      what is installed, and what it wants\n"
            "broker schemas                   every tool, as a model would be offered it\n"
            "broker match <sentence>          which module a sentence routes to\n"
            "broker call <module.tool> k=v …  run one tool"
        )
        return 0

    what, rest = argv[0], argv[1:]

    if what == "list":
        for module in registry.modules.values():
            print(f"{module.name} {module.version}  {module.summary}")
            print(f"  wants: {module.grants.spoken_summary()}")
            print(f"  says:  {module.grants.consequence}, confirm={module.grants.confirm}")
            print(f"  hears: {', '.join(module.phrases)}")
            print(f"  tools: {', '.join(t['function']['name'] for t in runner.schemas(module))}")
        for complaint in registry.broken:
            print(f"[skipped] {complaint}", file=sys.stderr)
        return 0

    if what == "schemas":
        every = [s for module in registry.modules.values() for s in runner.schemas(module)]
        print(json.dumps(every, indent=2))
        return 0

    if what == "match":
        said = " ".join(rest)
        module = registry.match(said)
        print(f"{said!r} -> {module.name if module else 'nothing'}")
        return 0

    if what == "call":
        if not rest:
            print("which tool?", file=sys.stderr)
            return 2
        name, _, tool = rest[0].partition(".")
        module = registry.get(name)
        if module is None or not tool:
            print(f"no module called {name!r}", file=sys.stderr)
            return 2
        args = {}
        for pair in rest[1:]:
            key, matched, value = pair.partition("=")
            if not matched:
                print(f"expected key=value, got {pair!r}", file=sys.stderr)
                return 2
            args[key] = value
        answer = runner.call(module, tool, args)
        print(f"{'ok' if answer.ok else 'failed'} in {answer.seconds}s")
        if answer.error:
            print(f"  error:  {answer.error}")
        if answer.speech:
            print(f"  speech: {answer.speech}")
        if answer.data:
            print(f"  data:   {json.dumps(answer.data)}")
        return 0 if answer.ok else 1

    print(f"no idea what {what!r} means", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
