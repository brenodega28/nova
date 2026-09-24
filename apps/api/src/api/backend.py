"""Holding a connection to the backend, including while there isn't one.

    backend = Backend()
    await backend.start()
    picture = await backend.command("state")
    async for event in backend.subscribe():
        ...

The backend is a separate program with a separate lifetime. It is started and
stopped by hand, it restarts itself when a setting changes, and it replaces its
own process image on a reload — so for this program, the backend being absent is
not a failure to report once and give up on, it is a state to sit in and keep
trying from. Everything here is built around that: connecting retries forever
with a backoff, commands fail cleanly rather than hanging when there is nobody to
answer them, and clients are told the connection dropped rather than being left
holding a stream that has quietly stopped.

The socket underneath carries two kinds of frame on one wire — replies, matched
to the command that asked by ``id``, and events, which nobody asked for. One
reader task takes them apart: replies go to the future waiting on that id, events
go to every subscriber. Nothing else reads the socket, so there is no race over
who gets the next line.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

log = logging.getLogger("nova.api.backend")

HOST = "127.0.0.1"
PORT = 8765
TOKEN = ""

COMMAND_TIMEOUT = 30.0
FIRST_RETRY = 0.5
LONGEST_RETRY = 10.0
QUEUED_EVENTS = 256
MAX_LINE = 1024 * 1024


class Unreachable(RuntimeError):
    """There is no backend on the other end right now."""


class Refused(RuntimeError):
    """The backend understood the command and would not do it."""


class Backend:
    """One connection to one backend, re-established whenever it drops."""

    def __init__(self, host: str = HOST, port: int = PORT, token: str = TOKEN):
        self.host = host
        self.port = port
        self.token = token
        self.connected = False
        self.last_error: str | None = None
        self.greeting: dict[str, Any] | None = None

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._waiting: dict[str, asyncio.Future] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None
        self._writing = asyncio.Lock()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._stay_connected())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        await self._drop("shutting down")

    def status(self) -> dict:
        """Whether there is a backend, without asking it anything."""
        return {
            "connected": self.connected,
            "host": self.host,
            "port": self.port,
            "last_error": self.last_error,
        }

    async def _stay_connected(self) -> None:
        """Connect, read until it breaks, wait, connect again. Forever."""
        pause = FIRST_RETRY
        while True:
            try:
                await self._connect()
                pause = FIRST_RETRY
                await self._read_forever()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.info("backend unreachable (%s); retrying in %.1fs", exc, pause)
            await self._drop(self.last_error or "connection closed")
            await asyncio.sleep(pause)
            pause = min(pause * 2, LONGEST_RETRY)

    async def _connect(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
        self.connected = True
        self.last_error = None
        log.info("connected to backend at %s:%s", self.host, self.port)

    async def _drop(self, why: str) -> None:
        """Tear the connection down and tell everyone still waiting on it."""
        was = self.connected
        self.connected = False
        self.greeting = None
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        for pending in self._waiting.values():
            if not pending.done():
                pending.set_exception(Unreachable(why))
        self._waiting.clear()
        if was:
            self._broadcast({"event": "disconnected", "data": {"reason": why}})

    async def _read_forever(self) -> None:
        assert self._reader is not None
        while True:
            line = await self._reader.readuntil(b"\n")
            if not line:
                raise ConnectionError("backend closed the connection")
            try:
                frame = json.loads(line)
            except ValueError:
                log.warning("backend sent something that was not JSON")
                continue
            if not isinstance(frame, dict):
                continue
            if "event" in frame:
                await self._on_event(frame)
            else:
                self._on_reply(frame)

    async def _on_event(self, frame: dict) -> None:
        if frame.get("event") == "challenge":
            await self._authenticate()
            return
        if frame.get("event") == "hello":
            self.greeting = frame.get("data")
        self._broadcast(frame)

    async def _authenticate(self) -> None:
        answer = await self.command("authenticate", {"token": self.token})
        log.info("authenticated with backend: %s", answer)

    def _on_reply(self, frame: dict) -> None:
        pending = self._waiting.pop(str(frame.get("id")), None)
        if pending is None or pending.done():
            return
        if frame.get("ok"):
            pending.set_result(frame.get("data") or {})
        else:
            pending.set_exception(Refused(frame.get("error") or "refused"))

    def _broadcast(self, frame: dict) -> None:
        """Hand an event to every listening client, dropping it for the stragglers.

        A websocket that has stopped draining must not be able to hold the whole
        stream up, and the newest state matters more to a dashboard than a
        complete replay, so a full queue loses its oldest entry.
        """
        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(frame)

    async def command(self, name: str, args: dict | None = None) -> dict:
        """Ask the backend to do one thing, and wait for its answer."""
        writer = self._writer
        if writer is None or not self.connected:
            raise Unreachable("the backend is not connected")

        identifier = str(next(self._ids))
        waiting: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[identifier] = waiting
        frame = json.dumps(
            {"id": identifier, "command": name, "args": args or {}}
        ).encode()

        try:
            async with self._writing:
                writer.write(frame + b"\n")
                await writer.drain()
            return await asyncio.wait_for(waiting, timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise Unreachable(f"the backend did not answer {name!r} in time") from exc
        except (OSError, ConnectionError) as exc:
            raise Unreachable(str(exc)) from exc
        finally:
            self._waiting.pop(identifier, None)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue]:
        """A queue of every event, for as long as the caller holds it."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUED_EVENTS)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
