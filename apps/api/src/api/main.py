"""The HTTP and WebSocket face of a backend that speaks neither.

    uv run nova-api

Two jobs, and deliberately no others. It translates the backend's control port
into something a phone can talk to, and it decides who is allowed to. Everything
it serves it learned by asking; it keeps no state of its own beyond the
connection, so restarting it costs nothing and loses nothing.

Keeping it out of the backend is what lets the backend stay a program about
audio. Auth, CORS, request lifetimes, a client that hangs up mid-response — all
of it lives here, next to the sixteen kilobytes of framework that knows how to
handle it, and none of it shares a process with an open microphone and two
Whisper models.

The backend not being there is an ordinary answer, not a server error. A
dashboard that opens while the backend is still loading its models should see
"not connected" and go on asking, which is why the routes that need it say 503
with a reason and the event stream stays open across the gap.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
from typing import Any

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .backend import Backend, Refused, Unreachable

log = logging.getLogger("nova.api")

HOST = os.environ.get("NOVA_API_HOST", "")
PORT = int(os.environ.get("NOVA_API_PORT", "8000"))
TOKEN = os.environ.get("NOVA_API_TOKEN", "")

BACKEND_HOST = os.environ.get("NOVA_BACKEND_HOST", "127.0.0.1")
BACKEND_PORT = int(os.environ.get("NOVA_BACKEND_PORT", "8765"))
BACKEND_TOKEN = os.environ.get("NOVA_CONTROL_TOKEN", "")

PING_SECONDS = 20.0

backend = Backend(BACKEND_HOST, BACKEND_PORT, BACKEND_TOKEN)


class Changes(BaseModel):
    """A settings change, as the dashboard sends it."""

    changes: dict[str, Any] = Field(min_length=1)
    apply: bool = False


class Reset(BaseModel):
    names: list[str] | None = None


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    await backend.start()
    try:
        yield
    finally:
        await backend.stop()


app = FastAPI(title="Nova API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


async def bearer(authorization: str | None = Header(default=None)) -> None:
    """Check the bearer token, when this API was given one to check.

    Compared in constant time, because a token checked with ``==`` leaks its
    length and then its contents to anything patient enough to time the replies.
    """
    if not TOKEN:
        return
    _, _, offered = (authorization or "").partition(" ")
    if not secrets.compare_digest(offered.strip(), TOKEN):
        raise HTTPException(status_code=401, detail="that token is not the right one")


async def ask(command: str, args: dict | None = None) -> dict:
    """Pass one command to the backend, turning its problems into HTTP ones."""
    try:
        return await backend.command(command, args)
    except Unreachable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Refused as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/health")
async def health() -> dict:
    """Is the API up, and can it see a backend. Never needs a token."""
    return {"ok": True, "backend": backend.status()}


@app.get("/state", dependencies=[Depends(bearer)])
async def state() -> dict:
    return await ask("state")


@app.get("/overview", dependencies=[Depends(bearer)])
async def overview() -> dict:
    """Everything the dashboard needs to draw itself from cold, in one call."""
    return await ask("hello")


@app.get("/settings", dependencies=[Depends(bearer)])
async def settings() -> dict:
    return await ask("settings")


@app.patch("/settings", dependencies=[Depends(bearer)])
async def change_settings(body: Changes) -> dict:
    return await ask("set", {"changes": body.changes, "apply": body.apply})


@app.post("/settings/reset", dependencies=[Depends(bearer)])
async def reset_settings(body: Reset) -> dict:
    return await ask("reset", {"names": body.names})


@app.get("/modules", dependencies=[Depends(bearer)])
async def modules() -> dict:
    return await ask("modules")


@app.post("/restart", dependencies=[Depends(bearer)])
async def restart() -> dict:
    return await ask("restart")


@app.post("/reload", dependencies=[Depends(bearer)])
async def reload() -> dict:
    return await ask("reload")


@app.websocket("/events")
async def events(socket: WebSocket, token: str = Query(default="")) -> None:
    """The live stream, held open whether or not the backend is there.

    The token arrives in the query string because a browser cannot put a header
    on a WebSocket handshake. It is checked before the socket is accepted, so an
    unauthorised client never sees a frame.

    The first message is always the current picture, so a dashboard that connects
    knows what it is looking at without a second round trip — and if the backend
    is down, it is told that instead and stays connected to hear it come back.
    """
    if TOKEN and not secrets.compare_digest(token, TOKEN):
        await socket.close(code=4401)
        return
    await socket.accept()

    async with backend.subscribe() as queue:
        try:
            await socket.send_json(
                {"event": "hello", "data": backend.greeting}
                if backend.greeting
                else {"event": "disconnected", "data": backend.status()}
            )
            while True:
                try:
                    frame = await asyncio.wait_for(queue.get(), timeout=PING_SECONDS)
                except asyncio.TimeoutError:
                    await socket.send_json({"event": "ping", "data": backend.status()})
                    continue
                await socket.send_json(frame)
        except WebSocketDisconnect:
            return
        except (RuntimeError, ConnectionError):
            return


def serve() -> None:
    """Run the API, refusing to expose an unauthenticated one to the network.

    Binding to every interface is what makes the dashboard reachable from a
    phone, and it is also what would make an unprotected restart button
    reachable from every other device on the same wifi. So the bind address is
    not a free choice: without a token it stays on loopback, and setting one is
    how you say you meant it.
    """
    import uvicorn

    host = HOST
    if not host:
        host = "0.0.0.0" if TOKEN else "127.0.0.1"
    if host != "127.0.0.1" and not TOKEN:
        raise SystemExit(
            "refusing to listen on "
            f"{host} without a token — set NOVA_API_TOKEN, or bind to 127.0.0.1"
        )
    if not TOKEN:
        log.warning("no NOVA_API_TOKEN set — listening on loopback only")

    uvicorn.run(app, host=host, port=PORT, log_level="info")


if __name__ == "__main__":
    serve()
