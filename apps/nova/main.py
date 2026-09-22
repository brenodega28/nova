import sys
import tomllib
import json
from pathlib import Path

MANIFEST = Path(__file__).with_name("module.toml")


class Module:
    def _load_manifest(self) -> dict:
        with MANIFEST.open("rb") as handle:
            return tomllib.load(handle)

    def _tool_schemas(self, manifest: dict) -> list[dict]:
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

    def run(self, argv: list[str] | None = None) -> int:
        argv = sys.argv[1:] if argv is None else argv
        manifest = self._load_manifest()

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
