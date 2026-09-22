#!/usr/bin/env python3
"""The weather module's entry point: one request in, one answer out.

    echo '{"tool": "current_conditions", "args": {"location": "Lisbon"}}' \\
        | python weather.py

    python weather.py current_conditions location=Lisbon
    python weather.py --schemas

A request is a single JSON object on stdin, ``{"tool": ..., "args": {...}}``. The
reply is a single JSON object on stdout carrying ``speech`` for the assistant to
say and ``data`` for anything that wants the numbers. Failures come back in that
same envelope rather than as a traceback or an exit code, because the thing
reading this is a broker collecting an answer, not a person reading a terminal.

Arguments are checked against ``module.toml`` before a tool sees them. The broker
will check them too, earlier and against the same file — this is not a substitute
for that, it is a module declining to trust its caller, which costs a schema walk
and means the manifest is enforced from the first day rather than the day the
broker lands.

Nothing here reaches for the network, the filesystem or a credential on its own.
The only egress is :func:`tools.fetch_json`, to the two hosts the manifest names.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

import tools

MANIFEST = Path(__file__).with_name("module.toml")


class BadRequest(ValueError):
    """The request did not match what the manifest allows."""


def load_manifest(path: Path = MANIFEST) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _default_for(spec: dict, manifest: dict):
    source = spec.get("default_from")
    if source:
        section, _, key = source.partition(".")
        return (manifest.get(section) or {}).get(key)
    return spec.get("default")


def _check_string(name: str, value, spec: dict) -> str:
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be text")
    value = value.strip()
    if not value:
        raise BadRequest(f"{name} cannot be empty")
    limit = spec.get("max_length")
    if limit and len(value) > limit:
        raise BadRequest(f"{name} must be {limit} characters or fewer")
    pattern = spec.get("pattern")
    if pattern and not re.fullmatch(pattern, value):
        raise BadRequest(f"{name} has characters I can't use")
    return value


def _check_integer(name: str, value, spec: dict) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise BadRequest(f"{name} must be a whole number") from None
    low, high = spec.get("minimum"), spec.get("maximum")
    if low is not None and value < low:
        raise BadRequest(f"{name} must be at least {low}")
    if high is not None and value > high:
        raise BadRequest(f"{name} must be at most {high}")
    return value


def _check_enum(name: str, value, spec: dict):
    allowed = spec.get("values") or []
    if value not in allowed:
        raise BadRequest(f"{name} must be one of {', '.join(map(str, allowed))}")
    return value


CHECKS = {"string": _check_string, "integer": _check_integer, "enum": _check_enum}


def validate(manifest: dict, tool: str, args: dict) -> dict:
    """Return the arguments a tool may be called with, or refuse the request."""
    tools_declared = manifest.get("tools") or {}
    if tool not in tools_declared:
        known = ", ".join(sorted(tools_declared)) or "none"
        raise BadRequest(f"no tool called {tool!r} — this module offers {known}")
    if not isinstance(args, dict):
        raise BadRequest("args must be an object")

    declared = (tools_declared[tool].get("args") or {})
    unknown = set(args) - set(declared)
    if unknown:
        raise BadRequest(f"{tool} takes no argument called {sorted(unknown)[0]!r}")

    checked = {}
    for name, spec in declared.items():
        if name in args and args[name] is not None:
            value = args[name]
        else:
            value = _default_for(spec, manifest)
            if value is None:
                if spec.get("required", True):
                    raise BadRequest(f"{tool} needs {name}")
                continue
        check = CHECKS.get(spec.get("type", "string"))
        if check is None:
            raise BadRequest(f"{name} has a type I don't know how to check")
        checked[name] = check(name, value, spec)
    return checked


def tool_schemas(manifest: dict) -> list[dict]:
    """The manifest, rewritten as tool definitions a model can be offered.

    Names, descriptions and bounds come from the one declaration the broker
    validates against, so what the model is told it may send and what it is
    allowed to send cannot drift apart.
    """
    types = {"string": "string", "integer": "integer", "enum": "string"}
    schemas = []
    for name, tool in (manifest.get("tools") or {}).items():
        properties, required = {}, []
        for arg, spec in (tool.get("args") or {}).items():
            field = {
                "type": types.get(spec.get("type", "string"), "string"),
                "description": spec.get("description", ""),
            }
            if spec.get("type") == "enum":
                field["enum"] = spec.get("values", [])
            if spec.get("minimum") is not None:
                field["minimum"] = spec["minimum"]
            if spec.get("maximum") is not None:
                field["maximum"] = spec["maximum"]
            properties[arg] = field
            if spec.get("required", True) and _default_for(spec, manifest) is None:
                required.append(arg)
        schemas.append(
            {
                "type": "function",
                "function": {
                    "name": f"{manifest['module']['name']}.{name}",
                    "description": tool.get("summary", ""),
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }
        )
    return schemas


def handle(request: dict, manifest: dict | None = None) -> dict:
    """Run one request and answer in the envelope, whatever happens."""
    manifest = manifest or load_manifest()
    tool = request.get("tool")
    try:
        args = validate(manifest, tool, request.get("args") or {})
    except BadRequest as exc:
        return {"ok": False, "tool": tool, "error": str(exc), "speech": str(exc)}

    timeout = float((manifest.get("grants") or {}).get("timeout_seconds", 10))
    try:
        result = tools.TOOLS[tool](timeout=timeout, **args)
    except tools.WeatherError as exc:
        return {"ok": False, "tool": tool, "error": str(exc), "speech": exc.spoken}
    except Exception as exc:
        return {
            "ok": False,
            "tool": tool,
            "error": f"{type(exc).__name__}: {exc}",
            "speech": "Something went wrong getting the weather.",
        }
    return {"ok": True, "tool": tool, **result}


def _request_from_argv(argv: list[str]) -> dict:
    args = {}
    for pair in argv[1:]:
        key, _, value = pair.partition("=")
        if not _:
            raise SystemExit(f"expected key=value, got {pair!r}")
        args[key] = value
    return {"tool": argv[0], "args": args}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    manifest = load_manifest()

    if argv and argv[0] == "--schemas":
        print(json.dumps(tool_schemas(manifest), indent=2))
        return 0

    if argv:
        request = _request_from_argv(argv)
    else:
        raw = sys.stdin.read()
        try:
            request = json.loads(raw)
        except ValueError:
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

    answer = handle(request, manifest)
    print(json.dumps(answer, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
