"""Separate WASAPI endpoints with a bounded, non-blocking monitoring queue.

PortAudio forces a duplex Stream into polling mode and sizes its output from
the capture buffer. Separate exclusive endpoints can use event-driven buffers.
This adapter is only for local monitoring, never recording or network audio.
"""

import contextlib
import time
from collections import deque

import numpy as np


class MonitorQueue:
    def __init__(self, blocksize, channels, max_blocks=4):
        # CPython deque append/popleft are atomic. Each callback owns its block
        # until it returns it to the pool; no lock spans a NumPy copy (which can
        # release the GIL). Neither callback waits for the other audio thread.
        self.blocksize = blocksize
        self.capacity = blocksize * max_blocks
        self.max_blocks = max_blocks
        self.ready = deque()
        self.free = deque(np.empty((blocksize, channels), dtype=np.float32)
                          for _ in range(max_blocks + 2))
        self.dropped_frames = self.underruns = self.contentions = 0
        self.last_wait_ms = None

    @property
    def size(self):
        return len(self.ready) * self.blocksize

    def push(self, samples):
        try:
            block = self.free.popleft()
        except IndexError:
            self.contentions += 1
            self.dropped_frames += len(samples)
            return
        np.copyto(block, samples)
        if len(self.ready) >= self.max_blocks:
            try:
                self.free.append(self.ready.popleft()[0])
                self.dropped_frames += self.blocksize
            except IndexError:
                pass  # The render callback consumed it first.
        self.ready.append((block, time.perf_counter()))

    def pop(self, output):
        try:
            block, enqueued_at = self.ready.popleft()
        except IndexError:
            self.last_wait_ms = None
            output.fill(0)
            self.underruns += 1
            return False
        try:
            self.last_wait_ms = round((time.perf_counter() - enqueued_at) * 1000, 3)
            np.copyto(output, block)
            return True
        finally:
            self.free.append(block)


class MirroredDuplexStream:
    """A direct duplex monitor plus an asynchronous virtual-microphone feed.

    The physical output stays in the original PortAudio duplex callback.  The
    second output only consumes copies from a bounded queue, so a slow shared
    virtual endpoint can drop its own frames but can never add latency to what
    the singer hears.
    """

    def __init__(self, sd, candidate, callback, statistics, failed, mirror_device):
        blocksize = int(candidate["blocksize"])
        channels = int(candidate["channels"][1])
        self.queue = MonitorQueue(blocksize, channels)
        self.statistics = statistics

        def local_callback(indata, outdata, frames, clocks, status):
            callback(indata, outdata, frames, clocks, status)
            if frames == blocksize:
                self.queue.push(outdata)

        def mirror_callback(outdata, frames, _clocks, status):
            if frames != blocksize:
                outdata.fill(0)
                self.statistics["virtual_microphone_bad_block"] = int(frames)
                return
            complete = self.queue.pop(outdata)
            if status or not complete:
                self.statistics["virtual_microphone_underruns"] = self.queue.underruns

        self.local = sd.Stream(**candidate, callback=local_callback)
        self.mirror = sd.OutputStream(
            samplerate=candidate["samplerate"],
            blocksize=blocksize,
            latency=candidate.get("latency", "low"),
            channels=channels,
            device=mirror_device,
            callback=mirror_callback,
        )

    @property
    def latency(self):
        return self.local.latency

    def start(self):
        try:
            self.mirror.start()
            self.local.start()
        except BaseException:
            self.abort()
            raise

    def abort(self):
        self._both("abort")

    def close(self):
        self._both("close")

    def _both(self, method):
        for stream in (self.local, self.mirror):
            with contextlib.suppress(Exception):
                getattr(stream, method)()


class WasapiMonitorStream:
    def __init__(
        self, sd, candidate, callback, statistics, failed, mirror_device=None
    ):
        rate = float(candidate["samplerate"])
        blocksize = int(candidate["blocksize"])
        if blocksize <= 0:
            raise ValueError("Split WASAPI monitoring requires a fixed callback size")
        self.queue = MonitorQueue(blocksize, candidate["channels"][1])
        self.mirror_queue = (
            MonitorQueue(blocksize, candidate["channels"][1])
            if mirror_device
            else None
        )
        self.input = self.output = self.mirror = None
        self.statistics = statistics
        self.rate = rate
        self.started_at = None
        self.late_underruns = 0
        self.primed = False
        # Exclusive render begins with silence while input/output endpoints
        # synchronize. Fade the first real samples in place so that a live
        # microphone signal cannot jump abruptly from zero to full amplitude.
        self.fade_curve = np.linspace(0.0, 1.0, max(2, int(rate * .005)), dtype=np.float32)
        self.fade_remaining = len(self.fade_curve)
        work = np.zeros((blocksize, candidate["channels"][1]), dtype=np.float32)

        def capture(indata, frames, clocks, status):
            # Fixed callbacks avoid allocation in the realtime path. Fail closed
            # if a driver violates the requested size instead of repeating audio.
            if frames != blocksize:
                failed.set()
                return
            try:
                callback(indata, work, frames, clocks, status)
                self.queue.push(work)
                if self.mirror_queue is not None:
                    self.mirror_queue.push(work)
            except Exception:
                # PortAudio otherwise swallows callback errors and can leave the
                # parent reporting a running stream with no microphone audio.
                failed.set()

        def render(outdata, frames, clocks, status):
            if frames != blocksize:
                outdata.fill(0)
                failed.set()
                return
            # Start with one spare callback block. Without it two independent
            # event callbacks can alternate between an empty and nonempty queue
            # even on the same interface. Re-prime after an actual starvation.
            if not self.primed and self.queue.size >= blocksize * 2:
                self.primed = True
            if self.primed:
                complete = self.queue.pop(outdata)
                self.primed = complete
            else:
                outdata.fill(0)
                self.queue.underruns += 1
                self.queue.last_wait_ms = None
                complete = False
            if complete and self.fade_remaining:
                offset = len(self.fade_curve) - self.fade_remaining
                count = min(frames, self.fade_remaining)
                outdata[:count] *= self.fade_curve[offset:offset + count, None]
                self.fade_remaining -= count
            elif not complete:
                self.fade_remaining = len(self.fade_curve)
            now = time.monotonic()
            if status:
                statistics["glitch_count"] = statistics.get("glitch_count", 0) + 1
            past_startup = self.started_at is not None and now - self.started_at > .3
            if past_startup and not complete:
                self.late_underruns += 1
            statistics.update(
                queue_frames=self.queue.size,
                queue_capacity_frames=self.queue.capacity,
                queue_underruns=self.queue.underruns,
                queue_underruns_after_start=self.late_underruns,
                queue_dropped_frames=self.queue.dropped_frames,
                queue_contentions=self.queue.contentions,
                queue_ms=round(self.queue.size * 1000 / rate, 2),
                queue_wait_ms=self.queue.last_wait_ms,
                queue_capacity_ms=round(self.queue.capacity * 1000 / rate, 2),
            )

        def mirror_render(outdata, frames, _clocks, status):
            if frames != blocksize:
                outdata.fill(0)
                statistics["virtual_microphone_bad_block"] = int(frames)
                return
            complete = self.mirror_queue.pop(outdata)
            if status or not complete:
                statistics["virtual_microphone_underruns"] = self.mirror_queue.underruns

        common = {key: value for key, value in candidate.items()
                  if key not in {"channels", "device", "extra_settings"}}
        try:
            self.input = sd.InputStream(**common, device=candidate["device"][0],
                                        channels=candidate["channels"][0],
                                        extra_settings=candidate["extra_settings"][0], callback=capture)
            self.output = sd.OutputStream(**common, device=candidate["device"][1],
                                          channels=candidate["channels"][1],
                                          extra_settings=candidate["extra_settings"][1], callback=render)
            if mirror_device:
                self.mirror = sd.OutputStream(
                    **common,
                    device=mirror_device,
                    channels=candidate["channels"][1],
                    extra_settings=candidate["extra_settings"][1],
                    callback=mirror_render,
                )
        except BaseException:
            self.close()
            raise

    @property
    def latency(self):
        return self.input.latency, self.output.latency

    def start(self):
        self.started_at = time.monotonic()
        try:
            if self.mirror is not None:
                self.mirror.start()
            self.input.start()
            self.output.start()
        except BaseException:
            self.abort()
            raise

    def abort(self):
        self._both("abort")

    def close(self):
        self._both("close")

    def _both(self, method):
        for stream in (self.mirror, self.output, self.input):
            if stream is not None:
                with contextlib.suppress(Exception):
                    getattr(stream, method)()
