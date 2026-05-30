"""
server.py - Local WebSocket server that bridges game events to the OBS overlay.

Architecture:
    game events (triggers.py)
        → event_bus.push(event)
        → FastAPI WebSocket broadcasts to all connected clients
        → overlay.html animates the notification

Why FastAPI + WebSockets over a simpler solution:
    - WebSockets give us true push (no polling lag)
    - FastAPI is async-native so it plays well with multiple connections
    - The overlay can reconnect automatically if OBS refreshes the source

Run this alongside main.py, or let main.py start it automatically.
"""

import asyncio
import json
import logging
import threading
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

log = logging.getLogger(__name__)

app = FastAPI()

# Set by the async startup handler once uvicorn's event loop is running.
# start_server_thread() blocks on this so callers know the server is truly ready.
_server_ready = threading.Event()

# ---------------------------------------------------------------------------
# Connection manager — tracks all connected overlay browser sources.
# There's usually only one (OBS), but supporting multiple is free.
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self._clients: list[WebSocket] = []
        self._lock = asyncio.Lock()
        # Startup event stored here so late-connecting clients still see it.
        self._startup_replay: dict | None = None

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.append(ws)
        log.info("Overlay connected (%d total)", len(self._clients))
        if self._startup_replay:
            try:
                await ws.send_text(json.dumps(self._startup_replay))
            except Exception:
                pass

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._clients:
                self._clients.remove(ws)
        log.info("Overlay disconnected (%d remaining)", len(self._clients))

    async def broadcast(self, event: dict) -> None:
        """Send an event to all connected overlays."""
        message = json.dumps(event)
        dead = []
        async with self._lock:
            clients = list(self._clients)
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Event bus — thread-safe bridge between sync bot code and async server
# ---------------------------------------------------------------------------

class EventBus:
    """
    Allows sync code (bot.py, triggers.py) to push events into the
    async FastAPI server without worrying about event loops.

    Usage:
        event_bus.push("relic", {"name": "Snecko Eye", "winrate": 28})
    """

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def push(self, event_type: str, data: dict[str, Any] = {}) -> None:
        if not self._loop or not self._loop.is_running():
            log.warning("Event bus not ready — dropping: %s", event_type)
            return
        event = {"type": event_type, **data}
        if event_type == "startup":
            manager._startup_replay = event
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(event), self._loop
        )
        log.debug("Event pushed: %s", event)


event_bus = EventBus()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.on_event("startup")
async def startup():
    event_bus.set_loop(asyncio.get_running_loop())
    _server_ready.set()
    log.info("Overlay server ready — WebSocket at ws://localhost:5000/ws")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # Keep connection alive; we only push, never receive
            await ws.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(ws)


@app.get("/")
async def serve_overlay():
    """Serve the overlay HTML directly — OBS points to http://localhost:5000"""
    overlay_path = Path(__file__).parent / "overlay.html"
    return HTMLResponse(overlay_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Start in a background thread (called from main.py)
# ---------------------------------------------------------------------------

def start_server_thread(host: str = "127.0.0.1", port: int = 5000) -> int:
    """
    Start uvicorn in a daemon thread. Blocks until the server is genuinely
    ready to accept connections before returning, so callers can rely on the
    event bus being live immediately after this returns.
    """
    import socket

    def _port_free(p: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, p))
                return True
            except OSError:
                return False

    # Find a free port
    while not _port_free(port):
        log.warning("Port %d in use — trying %d", port, port + 1)
        port += 1

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(
        target=server.run,
        name="overlay-server",
        daemon=True,
    )
    thread.start()

    if not _server_ready.wait(timeout=10):
        log.warning("Overlay server did not become ready within 10s.")
    else:
        log.info("Overlay server ready at http://%s:%d", host, port)

    return port
