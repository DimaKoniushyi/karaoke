"""Best-effort sender for processed monitor audio, used by monitor_worker.py.

push() is called from the realtime PortAudio/WASAPI callback thread and must
never block or perform network I/O itself -- samples are copied into a small
preallocated per-stream accumulator, and only a fully accumulated chunk
(~5ms) is ever handed off, as an already-encoded bytes object, to a bounded
queue. All actual socket I/O (including the initial connect) runs on a
separate thread, so a slow or unavailable relay server can never add latency
to the audio callback or to monitor_worker.py's startup sequence.
"""

from __future__ import annotations

import contextlib
import queue
import socket
import threading

import numpy as np

from .audio_relay_protocol import LIVE_RELAY_QUEUE_MAX_FRAMES, encode_frame

# A room duet is two-way live singing, not one-way speech -- every chunk here
# is latency the other participant hears added on top of the network/WebRTC
# path, in both directions. 15ms (a comfortable margin for plain speech) was
# audible as extra lag; 5ms costs more, smaller socket writes for the same
# audio but keeps this accumulator's own contribution close to a single
# audio block instead of a noticeable chunk of the whole budget.
_CHUNK_SECONDS = 0.005
_QUEUE_MAXSIZE = LIVE_RELAY_QUEUE_MAX_FRAMES


class _StreamAccumulator:
    __slots__ = ("buffer", "position")

    def __init__(self, size: int) -> None:
        self.buffer = np.empty(size, dtype=np.float32)
        self.position = 0


class RelayLink:
    def __init__(self, port: int, sample_rate: float, connect_timeout: float = 1.0) -> None:
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        self._sample_rate = sample_rate
        self._chunk_samples = max(1, round(sample_rate * _CHUNK_SECONDS))
        self._accumulators: dict[int, _StreamAccumulator] = {}
        self._closed = False
        self._socket: socket.socket | None = None
        self._connect_timeout = connect_timeout
        # Guards the _closed/_socket handoff between _connect_and_run (which
        # sets _socket once connected) and close() (which reads it to close
        # the real socket). Without this, both sides can each observe the
        # other's stale pre-write state and conclude "the other one will
        # close it" -- close() sees _socket still None and does nothing,
        # _connect_and_run sees _closed still False, publishes _socket, then
        # reads the sentinel close() already queued and returns without ever
        # closing the socket it just opened.
        self._state_lock = threading.Lock()
        self._thread = threading.Thread(target=self._connect_and_run, args=(port,), daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        return self._socket is not None

    def push(self, stream_id: int, sample_rate: float, samples: np.ndarray) -> None:
        if self._socket is None or self._closed:
            return
        accumulator = self._accumulators.get(stream_id)
        if accumulator is None:
            accumulator = _StreamAccumulator(self._chunk_samples)
            self._accumulators[stream_id] = accumulator
        source = np.asarray(samples, dtype=np.float32)
        offset, remaining = 0, len(source)
        while remaining:
            space = len(accumulator.buffer) - accumulator.position
            take = min(space, remaining)
            accumulator.buffer[accumulator.position : accumulator.position + take] = source[
                offset : offset + take
            ]
            accumulator.position += take
            offset += take
            remaining -= take
            if accumulator.position >= len(accumulator.buffer):
                self._enqueue(encode_frame(stream_id, sample_rate, accumulator.buffer))
                accumulator.position = 0

    def _enqueue(self, payload: bytes) -> None:
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(payload)

    def _connect_and_run(self, port: int) -> None:
        try:
            sock = socket.create_connection(("127.0.0.1", port), timeout=self._connect_timeout)
        except OSError:
            return
        sock.settimeout(None)
        with self._state_lock:
            if self._closed:
                with contextlib.suppress(OSError):
                    sock.close()
                return
            self._socket = sock
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    return
                try:
                    sock.sendall(item)
                except OSError:
                    return
        finally:
            # Without this, a send failure left self._socket set and
            # `connected` reporting True forever after -- push() kept
            # encoding and queueing audio for a sender thread that had
            # already exited and would never drain it again.
            with self._state_lock:
                if self._socket is sock:
                    self._socket = None
            with contextlib.suppress(OSError):
                sock.close()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
            sock = self._socket
        # Whatever's still sitting in each stream's accumulator (up to
        # _CHUNK_SECONDS worth) never reaches a full chunk otherwise -- push()
        # only ever enqueues once the buffer fills -- and was silently lost
        # on every stop/restart instead of being sent as one final short frame.
        for stream_id, accumulator in self._accumulators.items():
            if accumulator.position:
                self._enqueue(
                    encode_frame(stream_id, self._sample_rate, accumulator.buffer[: accumulator.position])
                )
                accumulator.position = 0
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        if sock is None:
            return
        # Give the sender thread a bounded chance to actually write the
        # flush frame (and anything else already queued) before force-
        # closing the socket out from under it -- closing it here
        # immediately, as this used to, could abort that data before
        # sendall() for it ever ran, silently dropping it anyway despite
        # the flush above.
        self._thread.join(timeout=1.0)
        with contextlib.suppress(OSError):
            sock.close()
