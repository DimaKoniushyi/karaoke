"""Wire format shared between monitor_worker.py (sender, child process) and
audio_relay.py (receiver, main process) for relaying processed monitor audio
to the browser. Deliberately separate from the existing stdin/stdout JSON
control channel (audio_service._launch_monitor_process opens that pipe in
text mode for command/status JSON lines; it cannot carry binary PCM).
"""

from __future__ import annotations

import struct

import numpy as np

STREAM_DRY = 0
STREAM_WET = 1

# All three live-relay hops use the same small budget. Dry and wet frames
# are interleaved, so four 5ms-per-stream frames represent roughly 10ms of
# wall-clock audio. A stalled consumer loses old audio instead of making a
# singer permanently late.
LIVE_RELAY_QUEUE_MAX_FRAMES = 4

# stream_id (0=dry, 1=wet), sample_rate, sample_count -- followed by
# sample_count * 4 bytes of little-endian float32 PCM. All three header
# fields are 4 bytes (stream_id doesn't need the range, but a 1-byte field
# left the 12-byte header un-aligned to 4 bytes) so the PCM payload always
# starts at a 4-byte-aligned offset -- letting a receiver construct a
# Float32Array directly over the received buffer instead of always copying
# the payload out first just to satisfy TypedArray alignment.
_HEADER = struct.Struct("<IfI")


def encode_frame(stream_id: int, sample_rate: float, samples: np.ndarray) -> bytes:
    payload = np.asarray(samples, dtype="<f4").tobytes()
    header = _HEADER.pack(int(stream_id), float(sample_rate), len(samples))
    return header + payload


class FrameReader:
    """Incrementally reassembles encode_frame() messages from a byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> None:
        self._buffer.extend(chunk)

    def pop_frames(self) -> list[tuple[int, float, np.ndarray]]:
        frames = []
        for raw in self.pop_raw_frames():
            stream_id, sample_rate, _sample_count = _HEADER.unpack_from(raw, 0)
            samples = np.frombuffer(raw, dtype="<f4", offset=_HEADER.size)
            frames.append((stream_id, sample_rate, samples))
        return frames

    def pop_raw_frames(self) -> list[bytes]:
        """Whole encode_frame() messages (header + PCM), undecoded.

        Lets a pure relay hop (audio_relay.py's AudioRelayServer) forward a
        frame exactly as received instead of decoding it into a tuple only to
        immediately re-encode an identical frame for the next hop.
        """
        frames = []
        while len(self._buffer) >= _HEADER.size:
            _stream_id, _sample_rate, sample_count = _HEADER.unpack_from(self._buffer, 0)
            total = _HEADER.size + sample_count * 4
            if len(self._buffer) < total:
                break
            frames.append(bytes(self._buffer[:total]))
            del self._buffer[:total]
        return frames
