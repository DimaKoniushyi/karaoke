"""Event-driven shared I/O. All COM calls stay on the monitor worker's thread."""
import ctypes as ct
import math
import sys
from pathlib import Path

import numpy as np


class Info(ct.Structure):
    _fields_ = [(name, ct.c_uint32) for name in (
        "sample_rate", "output_sample_rate", "blocksize", "input_period", "output_period", "input_buffer", "output_buffer"
    )] + [(name, ct.c_double) for name in ("input_latency_ms", "output_latency_ms")] + [
        (name, ct.c_uint32) for name in (
            "input_raw", "output_raw", "input_min_period", "output_min_period",
            "input_period_locked", "output_period_locked", "input_exclusive",
            "output_exclusive",
        )
    ]


TIMING_FIELDS = ("capture_delivery_ms", "program_residence_ms", "queue_residence_ms", "output_clock_lead_ms",
                 "render_submit_ms", "render_padding_ms", "capture_processing_ms", "event_wait_ms", "pump_gap_ms")


class Statistics(ct.Structure):
    _fields_ = [(name, ct.c_uint64) for name in (
        "captured_frames", "rendered_frames", "dropped_frames", "underruns", "discontinuities", "queued_frames"
    )] + [(name, ct.c_double) for name in ("stream_latency_ms", *TIMING_FIELDS, "resample_ratio")]


Process = ct.CFUNCTYPE(ct.c_int, ct.POINTER(ct.c_float), ct.POINTER(ct.c_float), ct.c_uint32)


def library_path():
    # Do not import application config here: it imports AI.__init__ and the
    # entire song pipeline into the latency-sensitive microphone worker.
    root = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[3] / "generated/build/asio")
    return root / "KaraokeWasapi.dll"


def load_library():
    path = library_path()
    if not path.is_file():
        raise RuntimeError("Native WASAPI library is missing; rebuild the native audio components")
    dll = ct.CDLL(str(path))
    try:
        version = dll.wm_abi_version
    except AttributeError as error:
        raise RuntimeError("Native WASAPI library is outdated; rebuild the native audio components") from error
    version.argtypes, version.restype = [], ct.c_uint32
    if version() != 7:
        raise RuntimeError("Native WASAPI library version mismatch; rebuild the native audio components")
    opening = [ct.c_wchar_p, ct.c_wchar_p, ct.c_wchar_p, ct.c_uint32, ct.c_float, ct.c_uint32,
               ct.POINTER(Info), ct.c_char_p, ct.c_uint32]
    dll.wm_open.argtypes, dll.wm_open.restype = opening, ct.c_void_p
    dll.wm_probe.argtypes, dll.wm_probe.restype = opening, ct.c_int
    dll.wm_start.argtypes, dll.wm_start.restype = [ct.c_void_p, Process, ct.c_char_p, ct.c_uint32], ct.c_int
    dll.wm_pump.argtypes, dll.wm_pump.restype = [ct.c_void_p, ct.c_uint32, ct.POINTER(Statistics), ct.c_char_p, ct.c_uint32], ct.c_int
    dll.wm_set_raw.argtypes, dll.wm_set_raw.restype = [ct.c_void_p, ct.c_int], None
    dll.wm_set_gain.argtypes, dll.wm_set_gain.restype = [ct.c_void_p, ct.c_float], None
    dll.wm_close.argtypes, dll.wm_close.restype = [ct.c_void_p], None
    return dll


class NativeWasapiStream:
    @classmethod
    def probe(cls, options):
        """Read endpoint periods without creating or starting a monitor stream."""
        dll = load_library()
        info = Info()
        error = ct.create_string_buffer(1024)
        success = dll.wm_probe(
            options["input_device_name"], options["output_device_name"],
            options.get("virtual_output_device_name", ""), options["blocksize"],
            float(options.get("gain", 1.0)), 1 if options.get("input_exclusive") else 0,
            ct.byref(info), error, len(error),
        )
        if not success:
            raise RuntimeError(
                error.value.decode("utf-8", errors="replace") or "Native WASAPI probe failed"
            )
        probe = cls.__new__(cls)
        probe.info = info
        return probe.diagnostics()

    def __init__(self, options, statistics):
        self.dll = load_library()
        self.info, self.stats = Info(), Statistics()
        self.error = ct.create_string_buffer(1024)
        self.statistics, self.callback = statistics, None
        # Empty names are allowed only for an explicitly requested system default.
        # Production supplies both resolved names; the DLL rejects ambiguity.
        self.handle = self.dll.wm_open(
            options["input_device_name"], options["output_device_name"],
            options.get("virtual_output_device_name", ""), options["blocksize"],
            float(options.get("gain", 1.0)), 1 if options.get("input_exclusive") else 0,
            ct.byref(self.info), self.error, len(self.error),
        )
        self._check(self.handle)

    def set_raw(self, raw: bool) -> None:
        # A momentary "listen to the raw voice" check: the native engine
        # skips calling back into Python at all for this block (see
        # Engine::raw_active in monitor.cpp) instead of running the full DSP
        # chain just to discard its result. Only monitor_worker.py calls this,
        # and only when it knows nothing (e.g. the room relay) still depends
        # on that Python callback running every block -- see main()'s
        # raw_eligible.
        if self.handle:
            self.dll.wm_set_raw(self.handle, 1 if raw else 0)

    def set_gain(self, gain: float) -> None:
        # Keeps the native raw pass-through's gain in sync with a live
        # volume-slider change -- otherwise it would stay pinned to whatever
        # was requested when the stream first opened.
        if self.handle:
            self.dll.wm_set_gain(self.handle, float(gain))

    def _check(self, success):
        if not success:
            raise RuntimeError(self.error.value.decode("utf-8", errors="replace") or "Native WASAPI failed")

    def start(self, callback):
        def process(source, output, frames):
            try:
                callback(np.ctypeslib.as_array(source, shape=(frames, 1)),
                         np.ctypeslib.as_array(output, shape=(frames, 1)), frames, None, None)
                return 1
            except Exception as error:
                # str(error) is "" for an exception raised with no message
                # (a bare `raise ValueError()`/failed `assert`) -- falling
                # back to repr() keeps pump()'s `if callback_error:` check
                # truthy so it still reports that a real callback exception
                # happened, instead of losing it to the generic device-error
                # text below just because this one had no message.
                self.statistics["callback_error"] = str(error) or repr(error)
                return 0
        self.callback = Process(process)
        self._check(self.dll.wm_start(self.handle, self.callback, self.error, len(self.error)))

    def pump(self):
        success = self.dll.wm_pump(self.handle, 20, ct.byref(self.stats), self.error, len(self.error))
        if not success:
            # process() (below) already captured the real Python exception
            # when the failure originated in the DSP callback -- monitor.cpp
            # only ever reports back the fixed string "Microphone processing
            # callback failed" for that case, which _check()'s generic error
            # text would otherwise surface instead, losing the actual reason.
            callback_error = self.statistics.get("callback_error")
            if callback_error:
                raise RuntimeError(callback_error)
            self._check(False)
        self.statistics.update(
            glitch_count=self.stats.discontinuities,
            queue_underruns=self.stats.underruns,
            queue_dropped_frames=self.stats.dropped_frames,
            queue_ms=round(self.stats.queued_frames * 1000 / self.info.sample_rate, 3),
            captured_frames=self.stats.captured_frames, rendered_frames=self.stats.rendered_frames,
            stream_latency_ms=round(self.stats.stream_latency_ms, 3) if self.stats.stream_latency_ms > 0 else None,
        )
        self.statistics.update({name: round(value, 3) if math.isfinite(value) and value >= 0 else None
                                for name in TIMING_FIELDS for value in (getattr(self.stats, name),)})
        self.statistics["resample_ratio"] = (
            self.stats.resample_ratio
            if math.isfinite(self.stats.resample_ratio) and self.stats.resample_ratio > 0
            else None
        )

    def diagnostics(self):
        input_rate = self.info.sample_rate
        output_rate = self.info.output_sample_rate
        minimum_ms = (
            self.info.input_min_period * 1000 / input_rate
            + self.info.output_min_period * 1000 / output_rate
            if input_rate and output_rate and self.info.input_min_period and self.info.output_min_period
            else None
        )
        negotiated_ms = (
            self.info.input_period * 1000 / input_rate
            + self.info.output_period * 1000 / output_rate
            if input_rate and output_rate and self.info.input_period and self.info.output_period
            else None
        )
        period_locked = bool(self.info.input_period_locked or self.info.output_period_locked)
        latency_limit = (
            "engine-period-locked"
            if period_locked and minimum_ms is not None and negotiated_ms is not None and negotiated_ms > minimum_ms
            else "driver-period"
            if minimum_ms is not None and minimum_ms > 16
            else "shared-low-latency-capable"
            if minimum_ms is not None
            else "unknown"
        )
        return {
            "engine": "wasapi-native-input-exclusive" if self.info.input_exclusive else "wasapi-native-shared",
            "host_api": "Windows WASAPI",
            "mode": "input-exclusive-output-shared" if self.info.input_exclusive else "shared",
            "exclusive": False, "input_exclusive": bool(self.info.input_exclusive),
            "output_exclusive": bool(self.info.output_exclusive),
            "input_raw": bool(self.info.input_raw), "output_raw": bool(self.info.output_raw),
            "blocksize": self.info.blocksize, "sample_rate": self.info.sample_rate,
            "output_sample_rate": self.info.output_sample_rate,
            "input_period_frames": self.info.input_period, "output_period_frames": self.info.output_period,
            "input_min_period_frames": self.info.input_min_period,
            "output_min_period_frames": self.info.output_min_period,
            "input_period_locked": bool(self.info.input_period_locked),
            "output_period_locked": bool(self.info.output_period_locked),
            "minimum_period_latency_ms": minimum_ms,
            "negotiated_period_latency_ms": negotiated_ms,
            "latency_limit": latency_limit,
            "input_latency_ms": self.info.input_latency_ms if self.info.input_latency_ms > 0 else None,
            "output_latency_ms": self.info.output_latency_ms if self.info.output_latency_ms > 0 else None,
            "latency_source": "wasapi-stream-report",
        }

    def close(self):
        if self.handle:
            self.dll.wm_close(self.handle)
            self.handle = None
            self.callback = None

    abort = close
