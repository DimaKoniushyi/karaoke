import socket
import time

import numpy as np

from app.services.audio_relay import AudioRelayServer
from app.services.audio_relay_protocol import STREAM_DRY, encode_frame


def wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_subscriber_receives_frames_sent_by_a_connected_client():
    server = AudioRelayServer()
    try:
        client = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
        try:
            subscriber = server.subscribe()
            samples = np.array([0.5, -0.5], dtype=np.float32)
            sent = encode_frame(STREAM_DRY, 48000.0, samples)
            client.sendall(sent)
            assert wait_until(lambda: not subscriber.empty())
            # A pure relay hop: the server forwards the exact undecoded bytes
            # it received (see AudioRelayServer.Frame), not a decoded tuple.
            assert subscriber.get(timeout=1.0) == sent
        finally:
            client.close()
    finally:
        server.close()


def test_frames_are_discarded_when_nobody_is_subscribed():
    server = AudioRelayServer()
    try:
        client = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
        try:
            client.sendall(encode_frame(STREAM_DRY, 48000.0, np.zeros(4, dtype=np.float32)))
            time.sleep(0.05)
        finally:
            client.close()
    finally:
        server.close()  # must not hang/raise even though nobody ever subscribed


def test_accepts_a_new_client_after_the_previous_one_disconnects():
    # A transient localhost disconnect (the monitor worker restarting for an
    # unrelated settings change) used to leave this server permanently dead
    # -- it only ever accepted one client for its whole lifetime.
    server = AudioRelayServer()
    try:
        first = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
        assert wait_until(lambda: server._client is not None)
        first.close()
        assert wait_until(lambda: server._client is None)

        second = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
        try:
            subscriber = server.subscribe()
            samples = np.array([0.25, -0.25], dtype=np.float32)
            sent = encode_frame(STREAM_DRY, 48000.0, samples)
            second.sendall(sent)
            assert wait_until(lambda: not subscriber.empty())
            assert subscriber.get(timeout=1.0) == sent
        finally:
            second.close()
    finally:
        server.close()


def test_close_does_not_wait_out_the_join_timeout_with_a_client_still_connected():
    # The accept loop's client.recv() has no timeout (unlike the listening
    # socket) -- without also closing the accepted client socket, close()
    # only unblocked once whatever was on the other end happened to close
    # its own socket first, up to the full 2s join timeout below every time.
    server = AudioRelayServer()
    client = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
    try:
        assert wait_until(lambda: server._client is not None)
        started = time.monotonic()
        server.close()
        assert time.monotonic() - started < 1.0
    finally:
        client.close()


def test_unsubscribe_stops_further_delivery():
    server = AudioRelayServer()
    try:
        client = socket.create_connection(("127.0.0.1", server.port), timeout=2.0)
        try:
            subscriber = server.subscribe()
            server.unsubscribe(subscriber)
            client.sendall(encode_frame(STREAM_DRY, 48000.0, np.zeros(4, dtype=np.float32)))
            time.sleep(0.05)
            assert subscriber.empty()
        finally:
            client.close()
    finally:
        server.close()
