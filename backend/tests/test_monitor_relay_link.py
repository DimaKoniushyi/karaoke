import socket
import threading
import time
from pathlib import Path

import numpy as np

from app.services.audio_relay_protocol import LIVE_RELAY_QUEUE_MAX_FRAMES, STREAM_DRY, FrameReader
from app.services.monitor_relay_link import RelayLink, _QUEUE_MAXSIZE


def wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def make_server():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    return server, server.getsockname()[1]


def test_live_relay_sender_never_retains_more_than_four_interleaved_frames():
    # Dry and wet frames share this queue. Four slots are about 10ms at the
    # 5ms per-stream chunk cadence; a larger backlog should be discarded,
    # never played late in a live duet.
    assert _QUEUE_MAXSIZE <= 4
    assert _QUEUE_MAXSIZE == LIVE_RELAY_QUEUE_MAX_FRAMES


def test_every_relay_hop_uses_the_same_low_latency_queue_budget():
    root = Path(__file__).resolve().parents[1]
    for relative in ("app/services/monitor_relay_link.py", "app/services/audio_service.py", "app/routers/audio_relay.py"):
        source = (root / relative).read_text(encoding="utf-8")
        assert "LIVE_RELAY_QUEUE_MAX_FRAMES" in source


def test_push_accumulates_and_flushes_a_full_chunk_once_the_threshold_is_reached():
    server, port = make_server()
    link = None
    client = None
    try:
        server.settimeout(2.0)
        link = RelayLink(port, sample_rate=1000.0)  # chunk = round(1000 * 0.005) = 5 samples
        client, _ = server.accept()
        client.settimeout(2.0)
        assert wait_until(lambda: link.connected)

        # 20 samples over a 5-sample chunk flushes exactly 4 full chunks.
        link.push(STREAM_DRY, 1000.0, np.ones(20, dtype=np.float32))

        reader = FrameReader()
        frames: list = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(frames) < 4:
            reader.feed(client.recv(4096))
            frames.extend(reader.pop_frames())
        assert len(frames) == 4
        stream_id, sample_rate, decoded = frames[0]
        assert stream_id == STREAM_DRY
        assert sample_rate == 1000.0
        assert len(decoded) == 5
    finally:
        if link is not None:
            link.close()
        if client is not None:
            client.close()
        server.close()


def test_close_flushes_a_partial_chunk_instead_of_dropping_it():
    # push() only ever enqueues once a stream's accumulator fills to a full
    # chunk -- whatever's left over (up to _CHUNK_SECONDS worth) was silently
    # lost on every stop/restart instead of being sent as one final short
    # frame.
    server, port = make_server()
    link = None
    client = None
    try:
        server.settimeout(2.0)
        link = RelayLink(port, sample_rate=1000.0)  # chunk = 5 samples
        client, _ = server.accept()
        client.settimeout(2.0)
        assert wait_until(lambda: link.connected)

        link.push(STREAM_DRY, 1000.0, np.ones(3, dtype=np.float32))  # short of a full chunk
        link.close()

        reader = FrameReader()
        frames: list = []
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not frames:
            chunk = client.recv(4096)
            if not chunk:
                break
            reader.feed(chunk)
            frames.extend(reader.pop_frames())
        assert len(frames) == 1
        stream_id, sample_rate, decoded = frames[0]
        assert stream_id == STREAM_DRY and sample_rate == 1000.0
        assert len(decoded) == 3
    finally:
        if link is not None:
            link.close()
        if client is not None:
            client.close()
        server.close()


def test_close_does_not_race_a_concurrent_push_from_another_thread():
    # push() runs on the realtime audio callback thread for the PortAudio-
    # based engines, and main() closes the relay before it stops that
    # stream -- close()'s accumulator flush loop and a still-running push()
    # used to mutate the same dict/buffers with no synchronization at all,
    # risking a "dictionary changed size during iteration" crash or a torn
    # flushed frame.
    server, port = make_server()
    link = None
    client = None
    stop = threading.Event()
    errors: list = []

    def hammer():
        stream_id = 0
        while not stop.is_set():
            try:
                link.push(stream_id, 1000.0, np.ones(3, dtype=np.float32))
            except Exception as exc:  # pragma: no cover - only on a real race
                errors.append(exc)
                return
            stream_id = (stream_id + 1) % 8  # churns new accumulator keys too

    try:
        server.settimeout(2.0)
        link = RelayLink(port, sample_rate=1000.0)
        client, _ = server.accept()
        client.settimeout(2.0)
        assert wait_until(lambda: link.connected)

        pusher = threading.Thread(target=hammer, daemon=True)
        pusher.start()
        time.sleep(0.05)
        link.close()
        stop.set()
        pusher.join(timeout=2.0)

        assert errors == []
    finally:
        stop.set()
        if link is not None:
            link.close()
        if client is not None:
            client.close()
        server.close()


def test_push_is_a_silent_no_op_before_the_connection_completes():
    link = RelayLink(port=1, sample_rate=1000.0, connect_timeout=0.05)
    try:
        link.push(STREAM_DRY, 1000.0, np.ones(4, dtype=np.float32))
    finally:
        link.close()
