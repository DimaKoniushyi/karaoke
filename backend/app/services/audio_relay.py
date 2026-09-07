"""Loopback TCP server that receives processed monitor audio from
monitor_worker.py's RelayLink and fans it out to subscribers (a WebSocket
route serving the browser). Purely additive: with no subscribers, incoming
frames are read and discarded -- draining the socket is still required so
the worker's writer thread never stalls on a full send buffer.
"""

from __future__ import annotations

import contextlib
import queue
import socket
import threading

from .audio_relay_protocol import FrameReader

# A whole undecoded encode_frame() message: this server is a pure byte relay
# between monitor_worker.py's RelayLink and the WebSocket route, so there is
# nothing to decode a frame INTO here -- see FrameReader.pop_raw_frames.
Frame = bytes


class AudioRelayServer:
    def __init__(self) -> None:
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self.port: int = self._server.getsockname()[1]
        self._subscribers: list[queue.Queue[Frame]] = []
        self._lock = threading.Lock()
        self._closed = False
        self._client: socket.socket | None = None
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def subscribe(self, maxsize: int = 32) -> "queue.Queue[Frame]":
        subscriber: queue.Queue[Frame] = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: "queue.Queue[Frame]") -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)

    def _accept_loop(self) -> None:
        self._server.settimeout(1.0)
        # A transient localhost disconnect (the monitor worker restarting
        # for an unrelated settings change, e.g.) used to leave this server
        # permanently dead -- accepting exactly one client for its whole
        # lifetime -- until the caller happened to build a brand new
        # AudioRelayServer. Loop back to accepting again after each client
        # disconnects instead, for as long as this server itself isn't
        # closed; only one client is ever expected at a time.
        while not self._closed:
            self._accept_one_client()

    def _accept_one_client(self) -> None:
        client = None
        while not self._closed and client is None:
            try:
                client, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
        if client is None:
            return
        client.settimeout(None)
        with self._lock:
            if self._closed:
                with contextlib.suppress(OSError):
                    client.close()
                return
            self._client = client
        reader = FrameReader()
        try:
            while not self._closed:
                chunk = client.recv(65536)
                if not chunk:
                    break
                reader.feed(chunk)
                for frame in reader.pop_raw_frames():
                    self._dispatch(frame)
        except OSError:
            pass
        finally:
            with contextlib.suppress(OSError):
                client.close()
            with self._lock:
                if self._client is client:
                    self._client = None

    def _dispatch(self, frame: Frame) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(frame)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    subscriber.get_nowait()
                with contextlib.suppress(queue.Full):
                    subscriber.put_nowait(frame)

    def close(self) -> None:
        self._closed = True
        with contextlib.suppress(OSError):
            self._server.close()
        # The accept loop blocks indefinitely on the already-connected
        # client's recv() (it has no timeout, unlike the listening socket
        # above) -- without also closing it here, close() only unblocks once
        # whatever is on the other end (the monitor worker's RelayLink)
        # happens to close its own socket first, up to the full join timeout
        # below. _stop_monitoring_process() calls this BEFORE tearing down
        # that worker, so every stop/restart used to eat close to 2s for it.
        with self._lock:
            client = self._client
        if client is not None:
            with contextlib.suppress(OSError):
                client.close()
        self._thread.join(timeout=2.0)
