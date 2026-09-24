# nova-api

The web face of a running backend. It holds one connection to the backend's
control port, and turns it into REST and a WebSocket a phone can use.

```
apps/backend          mic, Whisper, Piper, Ollama, modules
      │  127.0.0.1:8765, newline-delimited JSON
apps/api              REST + WebSocket, auth, CORS          ← you are here
      │  LAN, HTTP
apps/mobile           dashboard
```

It is a separate program on purpose. The backend has a microphone open, two
Whisper models resident and a realtime thread that must not be made to wait, and
none of that wants a web framework beside it. Everything with a request
lifecycle lives here instead.

It keeps no state of its own beyond the connection. Everything it serves it
learned by asking, so restarting it costs nothing and loses nothing — which is
the point of the two being separate instances.

## Running it

Start the backend first, or don't — the API waits for it either way.

```sh
uv run nova-api                    # loopback only, no token
NOVA_API_TOKEN=$(openssl rand -hex 16) uv run nova-api
```

Without a token it binds to `127.0.0.1` and will refuse to bind anywhere else,
because an unauthenticated restart button on a shared network is not a thing to
offer by accident. With one it binds `0.0.0.0`, which is what makes it reachable
from a phone.

| variable | default | meaning |
| --- | --- | --- |
| `NOVA_API_TOKEN` | none | bearer token clients must present |
| `NOVA_API_HOST` | loopback, or `0.0.0.0` with a token | address to bind |
| `NOVA_API_PORT` | `8000` | port to bind |
| `NOVA_BACKEND_HOST` | `127.0.0.1` | where the backend's control port is |
| `NOVA_BACKEND_PORT` | `8765` | that port |
| `NOVA_CONTROL_TOKEN` | none | token the backend wants, if it wants one |

## What it serves

Every route but `/health` wants `Authorization: Bearer <token>` when a token is
set. The WebSocket takes it as `?token=` instead, because a browser cannot put a
header on a handshake.

| route | does |
| --- | --- |
| `GET /health` | is the API up, and can it see a backend |
| `GET /overview` | everything needed to draw the dashboard from cold, in one call |
| `GET /state` | what she is doing, and the conversation so far |
| `GET /settings` | every setting, its value, its default, and a JSON Schema |
| `PATCH /settings` | change settings — `{"changes": {...}, "apply": true}` |
| `POST /settings/reset` | forget overrides — `{"names": [...]}`, or all of them |
| `GET /modules` | what is installed, what it wants reached, what it can do |
| `POST /restart` | rebuild the listener, applying settings |
| `POST /reload` | replace the backend's process image |
| `WS /events` | the live stream |

```sh
curl localhost:8000/health
curl -H "Authorization: Bearer $TOKEN" localhost:8000/overview
curl -X PATCH -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"changes": {"voice": "en_US-amy-medium"}, "apply": true}' \
     localhost:8000/settings
```

`/docs` is the generated OpenAPI page, which is the fastest way to try any of it.

## The backend not being there

It is a separate process with a separate lifetime: started by hand, restarting
itself when a setting changes, replacing its own process image on a reload. So
its absence is a state to sit in rather than an error to give up on.

- Connecting retries forever, backing off to ten seconds.
- Routes that need the backend answer `503` with the reason, not `500`.
- `/health` always answers, and says what it can see.
- The WebSocket stays open across the gap, sends `disconnected` when the backend
  goes, and `hello` with the whole picture again when it comes back.

A dashboard that opens while the models are still loading should show "not
connected" and keep asking. That is the normal first few seconds of a cold start.

## The event stream

The first frame is always the current picture — `hello` carrying the same thing
`/overview` returns, or `disconnected` if there is nothing to describe yet. So a
client that connects halfway through a conversation never has to reconstruct the
present from a stream that started in the middle.

After that, frames are `{"event": ..., "at": ..., "data": {...}}`:

`starting` `setup` `ready` `wake` `question` `thinking` `answer_chunk` `answer`
`module` `timeout` `error` `broken` `summary` `settings` `disconnected` `ping`

`answer_chunk` arrives a sentence at a time, as she speaks it, which is what lets
a dashboard fill the reply in at the speed it is being said rather than all at
once when it finishes.

A client that stops reading has its oldest events dropped rather than being
allowed to hold the stream up. The newest picture matters more to a dashboard
than a complete replay.
