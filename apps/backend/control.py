"""The port she listens on for instructions, and reports herself over.

    server = ControlServer(state, supervisor)
    server.start()

One TCP port, newline-delimited JSON, both directions. A client sends commands
and gets a reply carrying the same ``id`` it asked with; the backend pushes
events as they happen, unasked. It is the same envelope discipline
:mod:`broker` uses on modules — ``ok``, ``data``, ``error``, re-checked on the
way in — pointed the other way, at whatever is holding the socket.

Deliberately not HTTP. This process has a microphone open, two Whisper models
resident and a realtime thread that must not be made to wait, and none of that
wants a web framework, a request lifecycle or a TLS handshake living beside it.
The API is a separate program precisely so it can own those concerns; what
crosses this port is the smallest thing that lets it.

Two properties the API depends on. A client that connects halfway through a
conversation is sent ``hello`` with the entire current picture before any event,
so it never has to reconstruct the present from a stream that started in the
middle. And a client that stops reading is disconnected rather than allowed to
back the queue up into memory — a phone that went into a tunnel must not be able
to grow this process without bound.

Bound to the loopback address. The thing on this port can change which model she
thinks with and restart her at will, so it is not a thing to put on a network;
the API is what faces the network, and it is the API's job to have an opinion
about who may reach it.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

import broker
import languages
import persona
import settings as settings_module
import switch
import talk
from audio import Utterance
from hit_log import HitLog

HOST = "127.0.0.1"
PORT = 8765
TOKEN = os.environ.get("NOVA_CONTROL_TOKEN", "")

MAX_LINE = 64 * 1024
QUEUED_EVENTS = 256
WRITE_TIMEOUT = 5.0


class Refused(Exception):
    """The command cannot be carried out, for a reason worth sending back."""


class Client:
    """One connected socket, and the events it has not been sent yet.

    The queue is bounded and drops its oldest entry when full. A dashboard that
    fell behind wants the newest picture rather than a faithful replay of the
    backlog it missed, and the alternative — blocking the listener until a phone
    catches up — is not a trade anyone would choose.
    """

    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.authenticated = not TOKEN
        self.pending: list[str] = []
        self.alive = True
        self._ready = threading.Condition()

    def send(self, frame: dict) -> None:
        line = json.dumps(frame, ensure_ascii=False, default=str)
        with self._ready:
            if not self.alive:
                return
            if len(self.pending) >= QUEUED_EVENTS:
                self.pending.pop(0)
            self.pending.append(line)
            self._ready.notify()

    def drain(self) -> list[str]:
        with self._ready:
            while self.alive and not self.pending:
                self._ready.wait(timeout=1.0)
            lines, self.pending = self.pending, []
            return lines

    def close(self) -> None:
        with self._ready:
            self.alive = False
            self._ready.notify_all()


class ControlServer:
    """Accepts control connections, answers commands, broadcasts events."""

    def __init__(
        self,
        state,
        supervisor=None,
        host: str = HOST,
        port: int = PORT,
    ):
        self.state = state
        self.supervisor = supervisor
        self.host = host
        self.port = port
        self.clients: set[Client] = set()
        self._clients_lock = threading.Lock()
        self._server: socketserver.ThreadingTCPServer | None = None
        self._registry: broker.Registry | None = None
        self._runner = broker.Runner()

    def start(self) -> None:
        """Open the port on a thread of its own. Never blocks the caller."""
        handler = _handler_for(self)
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        with self._clients_lock:
            clients = list(self.clients)
        for client in clients:
            client.close()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()

    def attach(self, client: Client) -> None:
        with self._clients_lock:
            self.clients.add(client)

    def detach(self, client: Client) -> None:
        with self._clients_lock:
            self.clients.discard(client)
        client.close()

    def publish(self, event: str, data: dict | None = None) -> None:
        """Send one event to everyone attached and authenticated."""
        frame = _event_frame(event, data or {})
        with self._clients_lock:
            clients = list(self.clients)
        for client in clients:
            if client.authenticated:
                client.send(frame)

    def greeting(self) -> dict:
        """Everything a client needs before it has seen a single event."""
        return {
            "assistant": {"name": persona.NAME, "wake_word": persona.WAKE_WORD},
            "state": self.state.snapshot(),
            "settings": settings_module.describe(),
            "modules": self.modules(),
            "generation": self.supervisor.generation if self.supervisor else 0,
        }

    def modules(self) -> dict:
        """What is installed, what it wants, and what it can do.

        Reading the manifests runs nothing, so this is safe to answer on demand.
        Asking a module for its tool definitions does run it, which is why
        :class:`broker.Runner` holds the answer once it has it.
        """
        if self._registry is None:
            self._registry = broker.Registry()
        self._registry.discover()
        described = []
        for module in self._registry.modules.values():
            grants = module.grants
            described.append(
                {
                    "name": module.name,
                    "version": module.version,
                    "summary": module.summary,
                    "phrases": list(module.phrases),
                    "grants": {
                        "egress": list(grants.egress),
                        "read": list(grants.read),
                        "write": list(grants.write),
                        "secrets": list(grants.secrets),
                        "timeout": grants.timeout,
                        "consequence": grants.consequence,
                        "confirm": grants.confirm,
                        "summary": grants.spoken_summary(),
                    },
                    "tools": [
                        {
                            "name": tool["function"]["name"],
                            "description": tool["function"].get("description", ""),
                            "parameters": tool["function"].get("parameters", {}),
                        }
                        for tool in self._runner.schemas(module)
                    ],
                }
            )
        return {"installed": described, "broken": list(self._registry.broken)}

    def handle(self, client: Client, request: dict) -> dict:
        """Run one command and answer it, trusting nothing about the request."""
        name = request.get("command")
        if not isinstance(name, str):
            raise Refused("the request did not name a command")
        args = request.get("args") or {}
        if not isinstance(args, dict):
            raise Refused("args must be an object")

        if name == "authenticate":
            if TOKEN and args.get("token") != TOKEN:
                raise Refused("that token is not the one this backend was given")
            client.authenticated = True
            return {"authenticated": True}

        if not client.authenticated:
            raise Refused("authenticate first")

        handler = getattr(self, f"_do_{name}", None)
        if handler is None:
            raise Refused(f"no command called {name!r}")
        return handler(args)

    def _do_ping(self, args: dict) -> dict:
        return {"pong": True, "at": time.time()}

    def _do_state(self, args: dict) -> dict:
        return self.state.snapshot()

    def _do_hello(self, args: dict) -> dict:
        return self.greeting()

    def _do_settings(self, args: dict) -> dict:
        return settings_module.describe()

    def _do_modules(self, args: dict) -> dict:
        return self.modules()

    def _do_set(self, args: dict) -> dict:
        """Change settings, and say whether anything will happen before a restart."""
        changes = args.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise Refused("set needs a 'changes' object with at least one setting")

        known = settings_module.Settings.model_fields
        unknown = sorted(set(changes) - set(known))
        if unknown:
            raise Refused(f"there is no setting called {unknown[0]!r}")

        try:
            settings_module.store().write(changes)
        except Exception as exc:
            raise Refused(_why_it_will_not_take(exc)) from exc

        needs_restart = sorted(
            name for name in changes if settings_module.restart_required(name)
        )
        applied = False
        if args.get("apply") and needs_restart and self.supervisor is not None:
            self.supervisor.restart()
            applied = True

        described = settings_module.describe()
        self.publish("settings", described)
        return {
            "settings": described,
            "restart_required": needs_restart,
            "restarting": applied,
        }

    def _do_voices(self, args: dict) -> dict:
        language = _text_arg(
            args, "language", "voices needs a 'language', like 'en' or 'pt_BR'"
        )
        return {"language": language, "voices": talk.available_voices(language)}

    def _do_set_voice(self, args: dict) -> dict:
        voice = _text_arg(
            args, "voice", "set_voice needs a 'voice', like 'en_US-amy-medium'"
        )
        if voice not in talk.catalog():
            raise Refused(f"no voice named {voice!r} — see voices")
        self._switch_in_background("voice", switch.voice, voice)
        return {"switching": True, "voice": voice}

    def _do_set_language(self, args: dict) -> dict:
        code = _text_arg(
            args, "language", "set_language needs a 'language', like 'pt' or 'en'"
        ).lower()
        try:
            languages.name_of(code)
        except ValueError as exc:
            raise Refused(str(exc)) from exc
        self._switch_in_background("language", switch.language, code)
        return {"switching": True, "language": code}

    def _switch_in_background(
        self, what: str, action: Callable[[str], Any], value: str
    ) -> None:
        def run() -> None:
            try:
                action(value)
            except Exception as exc:
                self.publish(
                    "error",
                    {"message": f"could not switch {what} to {value!r}: "
                     f"{type(exc).__name__}: {exc}"},
                )
                return
            self.publish("settings", settings_module.describe())
            if self.supervisor is not None:
                self.supervisor.restart()

        threading.Thread(target=run, daemon=True).start()

    def _do_reset(self, args: dict) -> dict:
        names = args.get("names")
        if names is not None and not isinstance(names, list):
            raise Refused("names must be a list, or absent to reset everything")
        settings_module.store().clear(names)
        described = settings_module.describe()
        self.publish("settings", described)
        return {"settings": described}

    def _do_restart(self, args: dict) -> dict:
        if self.supervisor is None:
            raise Refused("this backend was started without a supervisor")
        self.supervisor.restart()
        return {"restarting": True, "generation": self.supervisor.generation}

    def _do_reload(self, args: dict) -> dict:
        if self.supervisor is None:
            raise Refused("this backend was started without a supervisor")
        self.supervisor.reload()
        return {"reloading": True}


def _event_frame(event: str, data: dict) -> dict:
    return {"event": event, "at": time.time(), "data": data}


def _text_arg(args: dict, key: str, missing: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise Refused(missing)
    return value.strip()


def _why_it_will_not_take(exc: Exception) -> str:
    """Turn a validation failure into one line somebody can act on."""
    if not isinstance(exc, ValidationError) or not exc.errors():
        return str(exc)
    first = exc.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "that setting"
    return f"{where}: {first['msg']}"


def _handler_for(server: "ControlServer"):
    class Handler(socketserver.BaseRequestHandler):
        """One connection: a thread reading commands, another writing events."""

        def handle(self) -> None:
            client = Client(self.request)
            self.request.settimeout(WRITE_TIMEOUT)
            server.attach(client)
            writer = threading.Thread(target=self._write, args=(client,), daemon=True)
            writer.start()
            try:
                if client.authenticated:
                    client.send(_event_frame("hello", server.greeting()))
                else:
                    client.send(_event_frame("challenge", {}))
                self._read(client)
            finally:
                server.detach(client)

        def _read(self, client: Client) -> None:
            """Read commands until the peer goes away.

            The socket carries a timeout so a stalled write cannot wedge the
            writer thread forever, and a timeout is per-socket rather than per
            call — so a quiet connection lands here, where it means nothing was
            said recently and emphatically not that the client has gone.
            """
            buffered = b""
            while client.alive:
                try:
                    chunk = self.request.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffered += chunk
                if len(buffered) > MAX_LINE:
                    client.send({"ok": False, "error": "that request is far too long"})
                    break
                while b"\n" in buffered:
                    line, _, buffered = buffered.partition(b"\n")
                    self._answer(client, line)

        def _answer(self, client: Client, line: bytes) -> None:
            if not line.strip():
                return
            try:
                request = json.loads(line)
            except ValueError:
                client.send({"ok": False, "error": "that was not JSON"})
                return
            if not isinstance(request, dict):
                client.send({"ok": False, "error": "a command must be an object"})
                return

            reply: dict[str, Any] = {"id": request.get("id")}
            try:
                reply.update({"ok": True, "data": server.handle(client, request)})
            except Refused as exc:
                reply.update({"ok": False, "error": str(exc)})
            except Exception as exc:
                reply.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            client.send(reply)

        def _write(self, client: Client) -> None:
            """Push queued events, dropping a client that has stopped reading.

            A peer that never drains its receive window would otherwise block
            ``sendall`` for as long as the connection survives. Timing out and
            hanging up is the right answer: the queue is already bounded, and a
            client this far behind has nothing to gain from the backlog.
            """
            while client.alive:
                for line in client.drain():
                    try:
                        self.request.sendall(line.encode("utf-8") + b"\n")
                    except (OSError, TimeoutError):
                        client.close()
                        return

    return Handler


class ControlLog(HitLog):
    """A :class:`HitLog` that puts every event on the wire.

    What the screen draws and what a dashboard is told are the same events, from
    the same source, which is the only way the two stay in agreement.
    """

    def __init__(self, server: ControlServer, wake_word: str, **kwargs):
        super().__init__(wake_word, **kwargs)
        self.server = server

    def starting(self) -> None:
        self.server.publish("starting", {})

    def setup(self, text: str) -> None:
        self.server.publish("setup", {"text": text})

    def ready(self, floor: float, threshold: float) -> None:
        self.server.publish("ready", {"noise_floor": floor, "gate": threshold})

    def scanned(self, utterance: Utterance, text: str) -> None:
        if self.verbose:
            self.server.publish("scanned", {"text": text})

    def wake(self, utterance: Utterance, text: str, latency: float) -> None:
        self.hits += 1
        self.server.publish(
            "wake", {"text": text, "latency": latency, "count": self.hits}
        )

    def question(self, utterance: Utterance | None, text: str) -> None:
        self.server.publish("question", {"text": text})

    def thinking(self) -> None:
        self.server.publish("thinking", {})

    def answer_chunk(self, text: str) -> None:
        self.server.publish("answer_chunk", {"text": text})

    def answer(self, text: str, elapsed: float) -> None:
        self.server.publish("answer", {"text": text, "seconds": elapsed})

    def used(self, module: str, tool: str, seconds: float) -> None:
        self.server.publish(
            "module", {"module": module, "tool": tool, "seconds": seconds}
        )

    def timeout(self) -> None:
        self.server.publish("timeout", {})

    def error(self, message: str) -> None:
        self.server.publish("error", {"message": message})

    def summary(self) -> None:
        self.server.publish("summary", {"wakes": self.hits})

    def broken(self, message: str) -> None:
        self.server.publish("broken", {"message": message})
