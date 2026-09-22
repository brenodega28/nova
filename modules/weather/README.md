# weather

Current conditions and a short forecast for anywhere in the world, from
[Open-Meteo](https://open-meteo.com) — no API key, no account, global coverage.

```
$ python weather.py current_conditions location=Lisbon
It's 22 degrees and clear in Lisbon.

$ python weather.py forecast location=Lisbon days=3
In Lisbon: Today, mainly clear, 21 to 31 degrees. Tomorrow, mainly clear,
20 to 33 degrees. Thursday, clear, 19 to 32 degrees.
```

| file | holds |
| --- | --- |
| `module.toml` | the manifest — identity, grants, tools, and every argument's bounds |
| `weather.py` | the entry point — stdio protocol, validation, dispatch |
| `tools.py` | the two tools, the WMO code table, and the one network seam |

## The contract

One JSON object in on stdin, one JSON object out on stdout.

```sh
echo '{"tool": "current_conditions", "args": {"location": "Lisbon"}}' | python weather.py
```

```json
{
  "ok": true,
  "tool": "current_conditions",
  "speech": "It's 22 degrees and clear in Lisbon.",
  "data": { "place": "Lisbon, Portugal", "temperature": 22, "conditions": "clear", … }
}
```

Failures arrive in the same envelope, never as a traceback or a non-zero exit:

```json
{ "ok": false, "tool": "current_conditions",
  "error": "no place called 'Qxzvbn'",
  "speech": "I couldn't find anywhere called Qxzvbn." }
```

`speech` is the sentence to say; `data` is for anything that wants the numbers.
The module words the facts rather than handing raw figures to the model, because a
model asked to phrase a temperature will eventually phrase one nobody measured.

## The manifest is the only source of truth

`module.toml` is read at every call. It decides which tools exist, what arguments
they take, and what those arguments may contain — and the same file generates the
tool definitions offered to the model:

```sh
python weather.py --schemas
```

So what the model is told it may send and what it is allowed to send come from one
declaration and cannot drift apart. Arguments are checked here as well as in the
broker: a module should not trust its caller either, it costs one schema walk, and
it means the manifest is enforced before the broker exists.

Everything outside the manifest is refused, with the reason spoken back:

| request | answer |
| --- | --- |
| `{"tool": "delete_everything"}` | no tool called 'delete_everything' |
| `{"args": {"sudo": "yes"}}` | forecast takes no argument called 'sudo' |
| `location: "Lisbon; rm -rf /"` | location has characters I can't use |
| `location: "Lisbon&latitude=0"` | location has characters I can't use |
| `location: "http://169.254.169.254/"` | location has characters I can't use |
| `days: 99` | days must be at most 7 |
| `units: "kelvin"` | units must be one of metric, imperial |

The `location` pattern is an allowlist of word characters, spaces and the
punctuation that real place names use, so `São Paulo`, `Zürich`, `N'Djamena` and
`Winston-Salem` all pass while anything that could reach into a URL, a shell or a
path does not. A place name only ever travels as a urlencoded query value, and the
hosts and paths are fixed in `tools.py` — never built from an argument.

## Settings

```toml
[settings]
home = "Lisbon"
units = "metric"
```

`home` is what "what's the weather?" means with no place named — **change this to
where you actually are.** Any argument with `default_from = "settings.home"` picks
it up, so it is set in one place.

## Grants

```toml
[grants]
egress = ["geocoding-api.open-meteo.com", "api.open-meteo.com"]
read = []
write = []
secrets = []
timeout_seconds = 10
consequence = "read-only"
confirm = false
```

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
  -v "$PWD:/skill:ro" -w /skill python:3.12-alpine python weather.py
```

Withhold the grant with `--network none` and it degrades rather than breaks:
*"I couldn't reach the weather service."*

## Known gaps

- **`egress` is declared, not yet enforced.** Nothing reads that list. A container
  gives you all-or-nothing networking; holding the module to those two hosts needs
  the broker to own egress and proxy it, which is the next piece to build.
- **`tools.fetch_json` is the seam.** It is the only function that touches the
  network, so when the broker takes egress over, it is the only one that changes.
- **No broker yet**, so `phrases`, `consequence` and `confirm` are declarations
  waiting for a reader.
