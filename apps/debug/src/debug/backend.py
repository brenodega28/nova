from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import time
from collections.abc import Callable

COMMAND_TIMEOUT = 10.0
FIRST_RETRY = 0.5
LONGEST_RETRY = 5.0
MAX_LINE = 4 * 1024 * 1024


class Unreachable(RuntimeError):
    pass


class Refused(RuntimeError):
    pass


class Backend:
    def __init__(self, host: str, port: int, token: str, on_event: Callable[[dict], None]):
        self.host = host
        self.port = port
        self.token = token
        self.on_event = on_event
        self.connected = False
        self.last_error: str | None = None
        self.greeting: dict | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._waiting: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._stay_connected())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._drop("shutting down", announce=False)

    async def _stay_connected(self) -> None:
        pause = FIRST_RETRY
        while True:
            try:
                reader, self._writer = await asyncio.open_connection(
                    self.host, self.port, limit=MAX_LINE
                )
                self.connected = True
                self.last_error = None
                pause = FIRST_RETRY
                self._emit("connected", {"host": self.host, "port": self.port})
                await self._read_forever(reader)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._drop(self.last_error or "connection closed")
            await asyncio.sleep(pause)
            pause = min(pause * 2, LONGEST_RETRY)

    def _drop(self, why: str, announce: bool = True) -> None:
        was = self.connected and announce
        self.connected = False
        self.greeting = None
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
        for pending in self._waiting.values():
            if not pending.done():
                pending.set_exception(Unreachable(why))
        self._waiting.clear()
        if was:
            self._emit("disconnected", {"reason": why})

    async def _read_forever(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readuntil(b"\n")
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if not isinstance(frame, dict):
                continue
            if "event" not in frame:
                self._on_reply(frame)
            elif frame["event"] == "challenge":
                asyncio.create_task(self.command("authenticate", {"token": self.token}))
            else:
                if frame["event"] == "hello":
                    self.greeting = frame.get("data")
                self.on_event(frame)

    def _on_reply(self, frame: dict) -> None:
        pending = self._waiting.pop(str(frame.get("id")), None)
        if pending is None or pending.done():
            return
        if frame.get("ok"):
            pending.set_result(frame.get("data") or {})
        else:
            pending.set_exception(Refused(frame.get("error") or "refused"))

    def _emit(self, event: str, data: dict) -> None:
        self.on_event({"event": event, "at": time.time(), "data": data})

    async def command(self, name: str, args: dict | None = None) -> dict:
        writer = self._writer
        if writer is None or not self.connected:
            raise Unreachable("the backend is not connected")
        identifier = str(next(self._ids))
        waiting: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[identifier] = waiting
        frame = json.dumps({"id": identifier, "command": name, "args": args or {}})
        try:
            writer.write(frame.encode() + b"\n")
            await writer.drain()
            return await asyncio.wait_for(waiting, timeout=COMMAND_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise Unreachable(f"the backend did not answer {name!r} in time") from exc
        except (OSError, ConnectionError) as exc:
            raise Unreachable(str(exc)) from exc
        finally:
            self._waiting.pop(identifier, None)
