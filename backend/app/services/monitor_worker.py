
from __future__ import annotations

import argparse
import contextlib
import json
import queue
import signal
import sys
import threading
import time
from typing import Any


def _stage(name: str) -> None:
    print(json.dumps({"event": "stage", "stage": name}), flush=True)


if __name__ == "__main__":
    _stage("import numpy")
import numpy as np  # noqa: E402 - startup stage telemetry must precede this costly import

if __name__ == "__main__":
    _stage("import sounddevice / initialize PortAudio")
try:
    import sounddevice as sd
except Exception:  # PortAudio may be unavailable in CI/diagnostics.
    from types import SimpleNamespace
    sd = SimpleNamespace(Stream=None, WasapiSettings=lambda **kwargs: kwargs)

if __name__ == "__main__":
    _stage("import microphone DSP")
from app.services.audio_relay_protocol import STREAM_DRY, STREAM_WET  # noqa: E402
from app.services.microphone_quality import (  # noqa: E402 - staged worker startup
    MonitorEffectsChain,
    RealtimePitchShifter,
    StudioMicrophoneProcessor,
)
from app.services.monitor_relay_link import RelayLink  # noqa: E402
from app.services.wasapi_monitor_stream import (  # noqa: E402
    MirroredDuplexStream,
    WasapiMonitorStream,
)

_running = True
_level = {"rms_db": -120.0, "clipping": False, "silent": True}
# Read every audio block by the realtime callback with no lock: only ever
# replaced wholesale (never mutated in place) by the single stdin-reader
# thread, so a lock would only ever protect the callback from a torn read
# that CPython's atomic name rebinding already rules out -- see
# _read_live_updates.
_live_params = {
    "volume": 1.0, "reverb": 0.0, "echo": 0.0, "delay": 0.0, "noise_suppression": 0.35, "octave": 0.0,
    "dry_monitor": 0.0,
}
# Populated by main() once the stream is chosen; read by _read_live_updates().
# "stream" is set for the native WASAPI engine regardless of relay (a live
# volume change must reach it either way); "raw_eligible" is only ever true
# with no relay attached -- see main()'s raw_eligible comment for why a
# room-relay session must never arm the raw pass-through.
_native_stream_target: dict[str, Any] = {"stream": None, "raw_eligible": False}


def _configure_realtime_python() -> None:
    # ctypes releases the GIL while wm_pump waits in native WASAPI and the
    # audio callback reacquires it for DSP.  Python's ordinary 5 ms thread
    # timeslice lets the stdin/report helper threads retain the GIL for most
    # of a 48/64-frame hardware period at the worst possible moment.  This
    # worker is a dedicated process, so a 1 ms slice bounds that scheduling
    # jitter without changing the main backend or UI interpreter.
    sys.setswitchinterval(0.001)


def _stream_candidate(options: dict) -> dict:
    """No buffer or rate fallback -- the requested blocksize/sample rate are
    used as-is. The public candidate remains shared/plain. Native WASAPI may
    separately request exclusive *capture* through ``input_exclusive`` while
    render stays shared; it never seizes the speakers/radio endpoint.
    """
    rate = float(options["sample_rate"])
    blocksize = int(options["blocksize"])
    if blocksize <= 0:
        raise ValueError("A fixed positive monitoring buffer is required")
    mode = options.get("wasapi_mode", "shared")
    if mode not in {"shared", "plain"}:
        raise ValueError("Unsupported WASAPI mode")
    candidate = {
        "samplerate": rate, "blocksize": blocksize, "latency": blocksize / rate,
        "channels": (1, int(options["output_channels"])),
        "device": (int(options["input_device_id"]), int(options["output_device_id"])),
        "_mode": mode,
    }
    if mode != "plain":
        candidate["extra_settings"] = (
            sd.WasapiSettings(exclusive=False, auto_convert=True),
            sd.WasapiSettings(exclusive=False, auto_convert=True),
        )
        if options.get("native_shared"):
            candidate["_engine"] = "wasapi-native-shared"
    return candidate

def _emit(payload: dict) -> None: print(json.dumps(payload), flush=True)

# The native WASAPI engine's pump loop runs on the same OS thread that
# Engine::start() gave MMCSS "Pro Audio" realtime priority (see monitor.cpp)
# -- a stdout print (JSON-encode + a flushing syscall) interleaved into that
# loop every ~100ms runs *on* the realtime thread, right between audio
# pumps. _queue_report hands the payload to a plain-priority thread instead;
# put/get are pure Python object shuffling, cheap enough for the pump loop.
_report_queue: queue.Queue[dict] = queue.Queue(maxsize=1)


def _queue_report(payload: dict) -> None:
    with contextlib.suppress(queue.Empty):
        _report_queue.get_nowait()  # drop a stale report rather than delay this one
    with contextlib.suppress(queue.Full):
        _report_queue.put_nowait(payload)


def _report_loop() -> None:
    while True:
        _emit(_report_queue.get())


def _stream_diagnostics(stream, candidate, options, mode):
    result = {
        "blocksize": candidate.get("blocksize", 0), "sample_rate": candidate.get("samplerate", options["sample_rate"]),
        "latency": candidate.get("latency", "low"), "mode": mode,
        "engine": "wasapi-split" if isinstance(stream, WasapiMonitorStream) else "duplex",
    }
    # WASAPI PortAudio derives these estimates from allocated buffer capacity,
    # not an observed mic-to-output transit time. Keep their provenance explicit.
    result["latency_source"] = "portaudio-buffer-estimate"
    latency = getattr(stream, "latency", None)
    if isinstance(latency, (tuple, list)) and len(latency) == 2:
        result.update(input_latency_ms=round(float(latency[0]) * 1000, 2),
                      output_latency_ms=round(float(latency[1]) * 1000, 2))
    return result


def _stop(_signum: int, _frame: object) -> None:
    global _running
    _running = False


def _read_live_updates() -> None:
    global _live_params
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line: continue
            try:
                update = json.loads(line)
            except json.JSONDecodeError:
                continue
            next_params = dict(_live_params)
            for key in ("volume", "reverb", "echo", "delay", "noise_suppression", "octave", "dry_monitor"):
                if key in update: next_params[key] = float(update[key])
            _live_params = next_params  # atomic rebind -- see module docstring above
            volume, dry_monitor = next_params.get("volume", 1.0), next_params.get("dry_monitor", 0.0) >= 0.5
            target = _native_stream_target
            stream = target["stream"]
            if stream is not None:
                if "volume" in update:
                    with contextlib.suppress(Exception):
                        stream.set_gain(volume)
                if "dry_monitor" in update and target["raw_eligible"]:
                    with contextlib.suppress(Exception):
                        stream.set_raw(dry_monitor)
    except Exception:
        return


def _update_level_report(level_source, time_info) -> None:
    rms, peak = (
        (
            float(np.sqrt(np.mean(np.square(level_source)))),
            float(np.max(np.abs(level_source))),
        )
        if len(level_source)
        else (0.0, 0.0)
    )
    adc_time = getattr(time_info, "inputBufferAdcTime", 0.0) or 0.0
    dac_time = getattr(time_info, "outputBufferDacTime", 0.0) or 0.0
    real_latency_ms = (
        (dac_time - adc_time) * 1000
        if adc_time and dac_time and dac_time > adc_time
        else None
    )
    _level.update(
        {
            "rms_db": round(20 * np.log10(rms) if rms > 0 else -120.0, 1),
            "clipping": peak >= 0.99,
            "silent": rms < 10 ** (-50 / 20),
            "real_latency_ms": (
                round(real_latency_ms, 3) if real_latency_ms is not None else None
            ),
        }
    )


def _audio_callback(gain: float, sample_rate: float = 44_100, statistics=None, relay: RelayLink | None = None):
    statistics = {} if statistics is None else statistics
    quality = StudioMicrophoneProcessor(sample_rate, 1)
    pitch = RealtimePitchShifter(sample_rate)
    effects = MonitorEffectsChain(sample_rate)
    # RMS/peak/real-latency are read by the UI at most a few times a second
    # (Settings polls monitor status every 750ms); computing them on every
    # single audio block (hundreds of times a second at a small buffer) is
    # pure waste on the realtime callback. A plain float in a one-item dict
    # survives across calls without a `nonlocal` on the inner function.
    level_state = {"reported_at": 0.0}
    _LEVEL_INTERVAL_SEC = 0.08
    def callback(indata, outdata, _frames, time_info, status):
        # First, unconditionally -- if anything below raises, the caller's
        # wrapper (see `callback` in main()) still reports failure and the
        # exception still propagates, but this specific output block is left
        # safely silent instead of playing back a stale/garbage buffer.
        outdata.fill(0)
        compute_started = time.perf_counter()
        statistics["callback_frames"] = int(_frames)
        statistics["callback_count"] = statistics.get("callback_count", 0) + 1
        if status:
            statistics["glitch_count"] = statistics.get("glitch_count", 0) + 1
        params = _live_params  # one atomic read of the current snapshot, no lock
        live_gain, reverb, echo, delay, noise_suppression, octave, dry_monitor = (
            params.get("volume", gain),
            params["reverb"],
            params["echo"],
            params["delay"],
            params.get("noise_suppression", 0.35),
            params.get("octave", 0.0),
            params.get("dry_monitor", 0.0) >= 0.5,
        )
        # A momentary "listen to the raw voice" check bypasses the whole
        # gate/compressor/tone-shaping/effects chain for what the singer
        # hears locally. With no relay/room peer listening either, nothing
        # needs the processed signal at all -- skip the DSP chain entirely
        # instead of computing it just to discard it. (This function is the
        # fallback PortAudio engine's callback; the native WASAPI engine
        # already skips calling it at all for this same case -- see
        # raw_active in monitor.cpp and _native_stream_target in main().)
        if dry_monitor and relay is None:
            monitor_output = np.clip(indata[:, 0] * live_gain, -1.0, 1.0).astype(np.float32)
            level_source = monitor_output
        else:
            processed = quality.process(indata[:, :1], live_gain, noise_suppression)[:, 0]
            dry = pitch.process(processed, octave)
            processed = effects.process(dry, reverb, echo, delay)
            if relay is not None:
                relay.push(STREAM_DRY, sample_rate, dry)
                relay.push(STREAM_WET, sample_rate, processed)
            monitor_output = (
                np.clip(indata[:, 0] * live_gain, -1.0, 1.0).astype(np.float32) if dry_monitor else processed
            )
            level_source = processed
        for channel in range(outdata.shape[1]): outdata[:, channel] = monitor_output
        if compute_started - level_state["reported_at"] >= _LEVEL_INTERVAL_SEC:
            level_state["reported_at"] = compute_started
            _update_level_report(level_source, time_info)
        statistics["dsp_compute_ms"] = round((time.perf_counter() - compute_started) * 1000, 3)

    return callback


def _live_options(options: dict[str, Any], gain: float) -> dict[str, float]:
    return {
        "volume": gain,
        "reverb": float(options.get("reverb", 0.0)),
        "echo": float(options.get("echo", 0.0)),
        "delay": float(options.get("delay", 0.0)),
        "noise_suppression": float(options.get("noise_suppression", 0.35)),
        "octave": float(options.get("octave", 0.0)),
        "dry_monitor": float(options.get("dry_monitor", 0.0)),
    }


def _pump_reports(stream, chosen_engine: str, failed: threading.Event, statistics) -> None:
    reported = time.monotonic()
    while _running and not failed.is_set():
        if chosen_engine == "wasapi-native-shared":
            stream.pump()
            if time.monotonic() - reported < 0.1:
                continue
            reported = time.monotonic()
        elif failed.wait(0.1):
            break
        raw_engaged = (
            _native_stream_target["raw_eligible"]
            and _live_params.get("dry_monitor", 0.0) >= 0.5
        )
        level_report = (
            {
                "rms_db": -120.0,
                "clipping": False,
                "silent": True,
                "real_latency_ms": None,
            }
            if raw_engaged
            else _level
        )
        reported_statistics = (
            {**statistics, "dsp_compute_ms": None}
            if raw_engaged
            else statistics
        )
        _queue_report({"event": "level", **level_report, **reported_statistics})
    if failed.is_set():
        raise RuntimeError(
            statistics.get(
                "callback_error",
                "Monitoring callback failed; selected settings were not changed",
            )
        )


def _configure_native_stream_target(stream, chosen_engine: str, relay) -> None:
    is_native = chosen_engine == "wasapi-native-shared"
    _native_stream_target["stream"] = stream if is_native else None
    _native_stream_target["raw_eligible"] = is_native and relay is None
    if (
        _native_stream_target["raw_eligible"]
        and _live_params.get("dry_monitor", 0.0) >= 0.5
    ):
        with contextlib.suppress(Exception):
            stream.set_raw(True)


def _open_portaudio_stream(options, candidate, callback, statistics, failed):
    engine = candidate.pop("_engine", "duplex")
    if engine == "wasapi-split":
        stream = WasapiMonitorStream(
            sd,
            candidate,
            callback,
            statistics,
            failed,
            mirror_device=options.get("virtual_output_device_name"),
        )
    elif options.get("virtual_output_device_name"):
        stream = MirroredDuplexStream(
            sd,
            candidate,
            callback,
            statistics,
            failed,
            options["virtual_output_device_name"],
        )
    else:
        stream = sd.Stream(**candidate, callback=callback)
    return stream, engine


def _read_options() -> dict:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    return json.loads(parser.parse_args().config)


def main() -> int:
    global _live_params
    _configure_realtime_python()
    options = _read_options()
    gain = float(options["gain"])
    _live_params = _live_options(options, gain)
    threading.Thread(target=_read_live_updates, daemon=True).start()
    threading.Thread(target=_report_loop, daemon=True).start()
    failed = threading.Event()
    statistics = {"glitch_count": 0}
    stream: Any = None
    relay: RelayLink | None = None
    try:
        candidate = _stream_candidate(options)
        _stage("initialize microphone DSP")
        relay_port = options.get("audio_relay_port")
        process = None

        def open_relay(sample_rate: float) -> None:
            nonlocal relay, process
            if relay is not None:
                relay.close()
            # Connecting is fully non-blocking (a background thread) specifically
            # so an unavailable or slow relay server can never delay solo
            # monitoring startup -- see monitor_relay_link.RelayLink.
            relay = RelayLink(int(relay_port), sample_rate) if relay_port else None
            process = _audio_callback(gain, sample_rate, statistics, relay)

        open_relay(float(options["sample_rate"]))

        def callback(*args):
            try:
                process(*args)
            except Exception as error:
                statistics["callback_error"] = str(error)
                failed.set()

        mode = candidate.pop("_mode")
        engine = candidate.get("_engine", "duplex")
        try:
            if engine == "wasapi-native-shared":
                _stage("load native WASAPI and open shared endpoints")
                from app.services.native_wasapi import NativeWasapiStream
                stream = NativeWasapiStream(options, statistics)
                if stream.info.sample_rate != float(options["sample_rate"]):
                    open_relay(stream.info.sample_rate)
                _stage("start native shared audio stream")
                stream.start(process)
                details = stream.diagnostics()
            else:
                _stage("open PortAudio stream")
                stream, engine = _open_portaudio_stream(
                    options, candidate, callback, statistics, failed
                )
                _stage("start PortAudio stream")
                stream.start()
                details = _stream_diagnostics(stream, candidate, options, mode)
            chosen_engine = engine
        except Exception:
            if stream is not None:
                for method in ("abort", "close"):
                    with contextlib.suppress(Exception):
                        getattr(stream, method)()
                stream = None
            raise
        _configure_native_stream_target(stream, chosen_engine, relay)
        _emit({"event": "started", **details})
        _pump_reports(stream, chosen_engine, failed, statistics)
    except Exception as exc:  # The parent converts this into a friendly API error.
        _emit({"event": "error", "message": str(exc)})
        return 1
    finally:
        if relay is not None:
            with contextlib.suppress(Exception):
                relay.close()
        if stream is not None:
            for method in ("abort", "close"):
                with contextlib.suppress(Exception):
                    getattr(stream, method)()
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGBREAK"): signal.signal(signal.SIGBREAK, _stop)
    sys.exit(main())
