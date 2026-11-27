"""WebSocket /ws/live: pushes the full live state whenever it changes (coalesced, at most
``ws_max_rate_hz`` messages per second) and a heartbeat every few seconds.

Message format: ``{"type": "live", "data": <same payload as GET /api/live>}``.
Full snapshots instead of diffs keep clients trivially consistent after reconnects; the
payload is ~30-60 kB for the whole plant, well within limits at a few messages per second.

Connections are refused (HTTP 403) when the browser's ``Origin`` is neither this host nor
one of ``CORS_ORIGINS`` (protects against other web pages opening the socket). Beyond
``WS_MAX_CLIENTS`` a connection is accepted and closed at once with code 1013 ("try again
later"), so the browser backs off instead of retrying immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from dmf.services.tracker_service import TrackerService

log = logging.getLogger(__name__)
router = APIRouter()

HEARTBEAT_S = 5.0
TRY_AGAIN_LATER = 1013


class Broadcaster:
    def __init__(self, service: TrackerService, max_rate_hz: float) -> None:
        self.service = service
        self.min_interval = 1.0 / max(max_rate_hz, 0.1)
        self.clients: set[asyncio.Queue[str]] = set()
        self._dirty = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self.service.add_listener(self._dirty.set)
        self._task = asyncio.create_task(self._loop(), name="ws-broadcast")

    async def stop(self) -> None:
        self.service.remove_listener(self._dirty.set)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def message(self) -> str:
        return json.dumps({"type": "live", "data": self.service.live_state()}, default=_default, separators=(",", ":"))

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=HEARTBEAT_S)
            except TimeoutError:
                pass
            self._dirty.clear()
            if self.clients:
                msg = self.message()
                for q in list(self.clients):
                    if q.full():  # slow client: drop its stale message, keep the newest
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    q.put_nowait(msg)
            await asyncio.sleep(self.min_interval)


def _default(o: Any) -> Any:
    if hasattr(o, "timestamp"):
        return o.timestamp()
    raise TypeError(f"not serialisable: {type(o)}")


def origin_allowed(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True  # not a browser (curl, monitoring): nothing to protect against
    settings = websocket.app.state.settings
    if origin in settings.cors_origin_list:
        return True
    host = websocket.headers.get("host", "")
    try:
        return urlsplit(origin).netloc.lower() == host.lower()
    except ValueError:
        return False


@router.websocket("/ws/live")
async def ws_live(websocket: WebSocket) -> None:
    broadcaster: Broadcaster = websocket.app.state.broadcaster
    if not origin_allowed(websocket):
        log.warning("websocket from foreign origin %r refused", websocket.headers.get("origin"))
        await websocket.close(code=1008)  # before accept: the handshake is answered with 403
        return
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    if len(broadcaster.clients) >= websocket.app.state.settings.ws_max_clients:
        await websocket.accept()
        await websocket.close(code=TRY_AGAIN_LATER, reason="too many clients, try again later")
        return
    broadcaster.clients.add(queue)  # reserve the slot BEFORE the first await (no race)
    try:
        await websocket.accept()
        await websocket.send_text(broadcaster.message())
        receiver = asyncio.create_task(_drain(websocket))
        while not receiver.done():
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                await websocket.send_text(getter.result())
            else:
                getter.cancel()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        broadcaster.clients.discard(queue)


async def _drain(websocket: WebSocket) -> None:
    """Read (and ignore) client messages - text or binary - so disconnects are noticed."""
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                return
    except (WebSocketDisconnect, RuntimeError):
        return
