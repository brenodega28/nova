# weather

Current conditions and a short forecast for anywhere in the world, from
[Open-Meteo](https://open-meteo.com) — no API key, no account, global coverage.

```
$ python main.py current_conditions location=Lisbon
It's 22 degrees and clear in Lisbon.

$ python main.py forecast location=Lisbon days=3
In Lisbon: Today, mainly clear, 21 to 31 degrees. Tomorrow, mainly clear,
20 to 33 degrees. Thursday, clear, 19 to 32 degrees.
```

Every module is a directory with a `main.py` and a `module.toml`. The entry point
is always `main.py`, so nothing has to declare where to start.

| file | holds |
| --- | --- |
| `module.toml` | the manifest — identity, grants, phrases, settings |
| `main.py` | the entry point — the two tools and the shapes they accept |
| `tools.py` | the Open-Meteo client, the WMO code table, the phrasing |

Everything else — manifest loading, argument checking, defaults, dispatch, the
answer envelope, the tool definitions — comes from `sdk.Module`.

## Writing the module

A tool is a method with its arguments declared beside it:

```python
from sdk import Choice, Module, Number, Text, tool

class Weather(Module):
    @tool("Conditions outside right now.", location=LOCATION, units=UNITS)
    def current_conditions(self, location: str, units: str) -> dict:
        return tools.current_conditions(location, units, self.timeout)
```

Those declarations are the only description of an argument that exists. They check
what arrives *and* generate what a model is offered:

```sh
python main.py --schemas
```

so what the model is told it may send and what it is allowed to send cannot drift
apart, and neither can drift from the signature.

## The contract

One JSON object in on stdin, one JSON object out on stdout.

```sh
echo '{"tool": "current_conditions", "args": {"location": "Lisbon"}}' | python main.py
```

```json
{
  "ok": true,
  "tool": "current_conditions",
  "speech": "It's 22 degrees and clear in Lisbon.",
  "data": { "place": "Lisbon, Portugal", "temperature": 22, "conditions": "clear" }
}
```

Failures arrive in the same envelope, never as a traceback or a non-zero exit —
raise `sdk.ModuleError(message, spoken)` and the runtime does the rest:

```json
{ "ok": false, "tool": "current_conditions",
  "error": "no place called 'Qxzvbn'",
  "speech": "I couldn't find anywhere called Qxzvbn." }
```

`speech` is the sentence to say; `data` is for anything that wants the numbers.
The module words the facts rather than handing raw figures to the model, because a
model asked to phrase a temperature will eventually phrase one nobody measured.

## What the manifest carries

```toml
manifest_version = 1

[module]
name = "weather"
phrases = ["weather", "forecast", …]

[grants]
egress = ["geocoding-api.open-meteo.com", "api.open-meteo.com"]
timeout_seconds = 10
consequence = "read-only"
confirm = false
```

Only what the host must know **without running a line of this module** — who it
is, what it wants reached, what it answers to. Deciding whether to trust a module
cannot require executing it, which is why grants live here and argument shapes
live in the code.

`manifest_version` is checked on load; a manifest from a future format is refused
rather than half-understood.

## Nothing outside the declaration gets through

| request | answer |
| --- | --- |
| `{"tool": "delete_everything"}` | no tool called 'delete_everything' |
| `{"args": {"sudo": "yes"}}` | forecast takes no argument called 'sudo' |
| `location: "Lisbon; rm -rf /"` | location has characters I can't use |
| `location: "Lisbon&latitude=0"` | location has characters I can't use |
| `location: "http://169.254.169.254/"` | location has characters I can't use |
| `days: 99` | days must be at most 7 |
| `units: "kelvin"` | units must be one of metric, imperial |

`PLACE` is the one piece of security this module owns, because only it knows what
its arguments are for: an allowlist of word characters, spaces and the punctuation
real place names use, so `São Paulo`, `Zürich`, `N'Djamena` and `Winston-Salem`
pass while anything that could reach into a URL, a shell or a path does not. A
place name only ever travels as a urlencoded query value, and hosts and paths are
fixed in `tools.py` — never built from an argument.

## Where the speaker is

`location` is required and has no default. This module does not know where anyone
is standing and does not try to guess — the assistant knows that, and passes it
in. The schema says as much, so a model calling the tool is told to supply it:

```json
"required": ["location"]
```

Asked for the weather with nowhere named, the module says so rather than
inventing a city:

```json
{ "ok": false, "error": "I'm not sure which place you mean.",
  "speech": "I'm not sure which place you mean." }
```

## Settings

```toml
[settings]
units = "metric"
```

Preferences the module owns, not facts about the speaker. Any argument declared
with `default_from = "settings.units"` picks this up, so it is set in one place.

## Grants

The module reads no files, writes none, holds no credential, and needs two hosts.
`consequence = "read-only"` is what lets it answer on voice alone — a module that
wrote, sent or spent anything would want `confirm = true` and a second channel.

It runs clean in the sandbox from the architecture note, as `nobody`, on a
read-only root, with no capabilities:

```sh
echo '{"tool":"current_conditions","args":{"location":"Lisbon"}}' | docker run --rm -i \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --user 65534:65534 --pids-limit 64 --memory 256m \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  -v "$PWD:/skill:ro" -v "$PWD/../../apps/sdk/src:/sdk:ro" \
  -e PYTHONPATH=/sdk -w /skill \
  python:3.12-alpine python main.py
```

The sandbox has to supply the runtime as well as the module — `sdk` goes in
read-only alongside it (or is installed into the sandbox image).

Withhold the grant with `--network none` and it degrades rather than breaks:
*"I couldn't reach the weather service."*

## Running it

The SDK is a package, so install it once and modules just import it:

```sh
uv pip install -e ../../apps/sdk
python main.py current_conditions location=Lisbon
```

## Known gaps

- **`egress` is declared, not yet enforced.** Nothing reads that list. Holding the
  module to those two hosts needs the host to own egress and proxy it — which under
  `bwrap --unshare-all` is the only way a module reaches anything at all.
- **`tools.fetch_json` is the seam.** The only function that touches the network,
  so it is the only one that changes when that happens.
- **No broker yet**, so `phrases`, `consequence` and `confirm` are declarations
  waiting for a reader.
