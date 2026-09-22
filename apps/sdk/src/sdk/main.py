"""The base every module is built on: manifest, arguments, protocol.

A module subclasses :class:`Module`, declares each tool with :func:`tool`, and
ships a thin ``module.toml`` beside itself. Everything else happens here, once,
for every module there will ever be — reading the manifest, filling in defaults,
checking arguments, dispatching, answering in the envelope, and emitting the tool
definitions a model can be offered.

    from sdk import Module, Text, tool

    class Weather(Module):
        @tool("What it is like outside right now.", location=Text(64))
        def current_conditions(self, location):
            return {"speech": "...", "data": {}}

    if __name__ == "__main__":
        raise SystemExit(Weather().run())

The split between the manifest and the code is deliberate. The manifest carries
what the host must know *without running a line of the module* — who it is, what
it wants reached, what it answers to — because deciding whether to trust a module
cannot require executing it first. Argument shapes live in the code, beside the
function they guard, where they cannot drift out of step with the signature.

Nothing here trusts its caller. A request arrives as JSON on stdin, every argument
is checked against what the tool declared, and anything unexpected comes back as a
spoken refusal rather than a traceback — what reads this is a broker collecting an
answer, not a person reading a terminal.
"""

from __future__ import annotations

import inspect
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any, Callable

MANIFEST_NAME = "module.toml"
SUPPORTED_MANIFEST = 1


class BadRequest(ValueError):
    """The request did not match what the module declared."""


class ModuleError(RuntimeError):
    """A tool failed in a way the speaker should hear about."""

    def __init__(self, message: str, spoken: str):
        super().__init__(message)
        self.spoken = spoken


class Arg:
    """One declared argument: how to check it, and how to describe it."""

    json_type = "string"

    def __init__(
        self,
        default: Any = None,
        default_from: str | None = None,
        description: str = "",
        required: bool = True,
    ):
        self.default = default
        self.default_from = default_from
        self.description = description
        self.required = required

    def check(self, name: str, value: Any) -> Any:
        return value

    def schema(self) -> dict:
        return {"type": self.json_type, "description": self.description}

    def optional(self) -> bool:
        return not self.required or self.default is not None or bool(self.default_from)


class Text(Arg):
    """Free text, bounded in length and held to a pattern."""

    def __init__(self, max_length: int = 256, pattern: str | None = None, **kwargs):
        super().__init__(**kwargs)
        self.max_length = max_length
        self.pattern = pattern

    def check(self, name: str, value: Any) -> str:
        if not isinstance(value, str):
            raise BadRequest(f"{name} must be text")
        value = value.strip()
        if not value:
            raise BadRequest(f"{name} cannot be empty")
        if len(value) > self.max_length:
            raise BadRequest(f"{name} must be {self.max_length} characters or fewer")
        if self.pattern and not re.fullmatch(self.pattern, value):
            raise BadRequest(f"{name} has characters I can't use")
        return value


class Choice(Arg):
    """One of a fixed set of values, and nothing else."""

    def __init__(self, *values: Any, **kwargs):
        super().__init__(**kwargs)
        self.values = list(values)

    def check(self, name: str, value: Any) -> Any:
        if value not in self.values:
            raise BadRequest(f"{name} must be one of {', '.join(map(str, self.values))}")
        return value

    def schema(self) -> dict:
        return {**super().schema(), "enum": self.values}


class Number(Arg):
    """A whole number inside a range."""

    json_type = "integer"

    def __init__(
        self, minimum: int | None = None, maximum: int | None = None, **kwargs
    ):
        super().__init__(**kwargs)
        self.minimum = minimum
        self.maximum = maximum

    def check(self, name: str, value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise BadRequest(f"{name} must be a whole number") from None
        if self.minimum is not None and value < self.minimum:
            raise BadRequest(f"{name} must be at least {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise BadRequest(f"{name} must be at most {self.maximum}")
        return value

    def schema(self) -> dict:
        described = super().schema()
        if self.minimum is not None:
            described["minimum"] = self.minimum
        if self.maximum is not None:
            described["maximum"] = self.maximum
        return described


class ToolSpec:
    """A tool as declared: what it is for, and what it accepts."""

    def __init__(self, name: str, summary: str, args: dict[str, Arg]):
        self.name = name
        self.summary = summary
        self.args = args


def tool(summary: str, **args: Arg) -> Callable:
    """Mark a method as a tool, with the arguments it will accept.

    The summary is what a model is told the tool is for, so write it for a reader
    deciding whether to call it.
    """

    def declare(function: Callable) -> Callable:
        setattr(function, "tool_spec", ToolSpec(function.__name__, summary, args))
        return function

    return declare


class Module:
    """What a module inherits: everything except its own subject matter."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        declared = {}
        for name in dir(cls):
            spec = getattr(getattr(cls, name, None), "tool_spec", None)
            if spec is not None:
                declared[spec.name] = spec
        cls.tools = declared

    def __init__(self, manifest_path: Path | None = None):
        self.manifest_path = manifest_path or self._beside_subclass()
        self.manifest = self._load_manifest()

    def _beside_subclass(self) -> Path:
        """The manifest sits next to the file that defined the subclass."""
        return Path(inspect.getfile(type(self))).resolve().with_name(MANIFEST_NAME)

    def _load_manifest(self) -> dict:
        with self.manifest_path.open("rb") as handle:
            manifest = tomllib.load(handle)
        version = manifest.get("manifest_version")
        if version != SUPPORTED_MANIFEST:
            raise ValueError(
                f"{self.manifest_path.name} declares manifest_version {version!r}; "
                f"this runtime speaks {SUPPORTED_MANIFEST}"
            )
        return manifest

    @property
    def name(self) -> str:
        declared = (self.manifest.get("module") or {}).get("name")
        return declared or type(self).__name__.lower()

    @property
    def settings(self) -> dict:
        return self.manifest.get("settings") or {}

    @property
    def grants(self) -> dict:
        return self.manifest.get("grants") or {}

    @property
    def timeout(self) -> float:
        return float(self.grants.get("timeout_seconds", 10))

    def _default_for(self, spec: Arg) -> Any:
        """Where an argument comes from when the speaker did not give one."""
        if spec.default_from:
            section, _, key = spec.default_from.partition(".")
            return (self.manifest.get(section) or {}).get(key)
        return spec.default

    def validate(self, name: str, args: dict) -> dict:
        """Return the arguments a tool may be called with, or refuse the request."""
        if name not in self.tools:
            known = ", ".join(sorted(self.tools)) or "none"
            raise BadRequest(f"no tool called {name!r} — this module offers {known}")
        if not isinstance(args, dict):
            raise BadRequest("args must be an object")

        declared = self.tools[name].args
        unknown = set(args) - set(declared)
        if unknown:
            raise BadRequest(f"{name} takes no argument called {sorted(unknown)[0]!r}")

        checked = {}
        for arg, spec in declared.items():
            if arg in args and args[arg] is not None:
                value = args[arg]
            else:
                value = self._default_for(spec)
                if value is None:
                    if not spec.optional():
                        raise BadRequest(f"{name} needs {arg}")
                    continue
            checked[arg] = spec.check(arg, value)
        return checked

    def schemas(self) -> list[dict]:
        """The declared tools, rewritten as definitions a model can be offered.

        Names, descriptions and bounds come from the one declaration that is also
        enforced, so what the model is told it may send and what it is allowed to
        send cannot drift apart.
        """
        described = []
        for name, spec in sorted(self.tools.items()):
            properties, required = {}, []
            for arg, declared in spec.args.items():
                properties[arg] = declared.schema()
                if not declared.optional() and self._default_for(declared) is None:
                    required.append(arg)
            described.append(
                {
                    "type": "function",
                    "function": {
                        "name": f"{self.name}.{name}",
                        "description": spec.summary,
                        "parameters": {
                            "type": "object",
                            "properties": properties,
                            "required": required,
                        },
                    },
                }
            )
        return described

    def handle(self, request: dict) -> dict:
        """Run one request and answer in the envelope, whatever happens."""
        name = request.get("tool")
        if not isinstance(name, str):
            refusal = "the request did not name a tool"
            return {"ok": False, "tool": None, "error": refusal, "speech": refusal}
        try:
            args = self.validate(name, request.get("args") or {})
        except BadRequest as exc:
            return {"ok": False, "tool": name, "error": str(exc), "speech": str(exc)}

        try:
            result = getattr(self, name)(**args)
        except ModuleError as exc:
            return {"ok": False, "tool": name, "error": str(exc), "speech": exc.spoken}
        except Exception as exc:
            return {
                "ok": False,
                "tool": name,
                "error": f"{type(exc).__name__}: {exc}",
                "speech": f"Something went wrong in {self.name}.",
            }
        return {"ok": True, "tool": name, **result}

    @staticmethod
    def _request_from_argv(argv: list[str]) -> dict:
        args = {}
        for pair in argv[1:]:
            key, matched, value = pair.partition("=")
            if not matched:
                raise SystemExit(f"expected key=value, got {pair!r}")
            args[key] = value
        return {"tool": argv[0], "args": args}

    def _request_from_stdin(self) -> dict | None:
        try:
            return json.loads(sys.stdin.read())
        except ValueError:
            return None

    def run(self, argv: list[str] | None = None) -> int:
        argv = sys.argv[1:] if argv is None else argv

        if argv and argv[0] == "--schemas":
            print(json.dumps(self.schemas(), indent=2))
            return 0
        if argv and argv[0] == "--manifest":
            print(json.dumps(self.manifest, indent=2, default=str))
            return 0

        if argv:
            request = self._request_from_argv(argv)
        else:
            request = self._request_from_stdin()
            if request is None:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "error": "the request was not JSON",
                            "speech": "I couldn't read that request.",
                        }
                    )
                )
                return 0

        print(json.dumps(self.handle(request), ensure_ascii=False))
        return 0
