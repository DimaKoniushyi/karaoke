"""WebSocket bridge of processed monitor audio to the browser, for the
WebRTC room (see the DSP-unification plan, Task A). A pure byte-pump: it
does not decode, transform, or make sense of the audio itself -- it only
forwards whatever app.services.audio_service.subscribe_monitor_relay()
hands it, unchanged -- the exact bytes monitor_worker.py's RelayLink sent,
never decoded and re-encoded along the way (see AudioRelayServer.Frame).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import queue
import threading

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services import audio_service
from app.services.audio_relay_protocol import LIVE_RELAY_QUEUE_MAX_FRAMES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/audio", tags=["audio"])

# Read the same way app.main reads it for the HTTP X-ADVoice-Token check.
# Recomputed here (rather than imported from app.main) to avoid a circular
# import -- app.main.app.include_router(audio_relay.router) imports this
# module, so this module cannot import back from app.main.
_API_TOKEN = os.environ.get("SONGAPP_API_TOKEN", "")

# A browser WebSocket constructor cannot set the X-ADVoice-Token header every
# other endpoint uses, so the launch token travels as a query parameter for
# this one connection instead -- see audio_service.subscribe_monitor_relay.
_NO_RELAY_CLOSE_CODE = 4004
_POLL_TIMEOUT_SECONDS = 1.0


def _token_is_valid(token: str | None) -> bool:
    if not _API_TOKEN:
        return True
    return bool(token) and hmac.compare_digest(token, _API_TOKEN)


@router.websocket("/direct-monitor/relay")
async def monitor_relay(websocket: WebSocket) -> None:
    if not _token_is_valid(websocket.query_params.get("token")):
        await websocket.close(code=1008)
        return

    subscription = audio_service.subscribe_monitor_relay()
    if subscription is None:
        await websocket.accept()
        await websocket.close(code=_NO_RELAY_CLOSE_CODE, reason="monitor unavailable")
        return

    relay, subscriber = subscription
    await websocket.accept()

    # One dedicated thread for this connection's whole lifetime, instead of
    # asyncio.to_thread dispatching a fresh threadpool work item for every
    # single relay frame (at RelayLink's ~5ms dry+wet chunking, hundreds of
    # dispatches a second just to move a queued item onto the event loop).
    # The thread only ever blocks on the plain queue.Queue and hands frames
    # to the loop via call_soon_threadsafe; outgoing (a real asyncio.Queue)
    # is what pump_frames() actually awaits.
    loop = asyncio.get_running_loop()
    outgoing: asyncio.Queue[bytes] = asyncio.Queue(maxsize=LIVE_RELAY_QUEUE_MAX_FRAMES)
    stop_pulling = threading.Event()

    def offer(frame: bytes) -> None:
        try:
            outgoing.put_nowait(frame)
        except asyncio.QueueFull:
            with contextlib.suppress(asyncio.QueueEmpty):
                outgoing.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                outgoing.put_nowait(frame)

    def pull_frames() -> None:
        while not stop_pulling.is_set():
            try:
                frame = subscriber.get(True, _POLL_TIMEOUT_SECONDS)
            except queue.Empty:
                continue
            loop.call_soon_threadsafe(offer, frame)

    puller = threading.Thread(target=pull_frames, daemon=True)
    puller.start()

    async def pump_frames() -> None:
        while True:
            await websocket.send_bytes(await outgoing.get())

    async def wait_for_disconnect() -> None:
        with contextlib.suppress(WebSocketDisconnect):
            while True:
                await websocket.receive()

    pump_task = asyncio.create_task(pump_frames())
    disconnect_task = asyncio.create_task(wait_for_disconnect())
    try:
        await asyncio.wait({pump_task, disconnect_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_pulling.set()
        pump_task.cancel()
        disconnect_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await pump_task
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await disconnect_task
        relay.unsubscribe(subscriber)
        with contextlib.suppress(Exception):
            await websocket.close()
