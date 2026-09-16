"""Bounded, silent WASAPI probes. Never stores or plays microphone samples."""

import argparse
import ctypes
import json
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.wasapi_monitor_stream import WasapiMonitorStream


def host_buffer_frames(stream):
    """Use the optional WASAPI extension from the exact DLL loaded by sounddevice."""
    try:
        library = ctypes.CDLL(sd._libname)
        query = library.PaWasapi_GetFramesPerHostBuffer
        query.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        query.restype = ctypes.c_int
        input_frames, output_frames = ctypes.c_uint(), ctypes.c_uint()
        pointer = int(sd._ffi.cast("uintptr_t", stream._ptr))
        error = query(pointer, ctypes.byref(input_frames), ctypes.byref(output_frames))
        if error:
            return {"host_buffer_query_error": error}
        return {"input_host_buffer_frames": input_frames.value, "output_host_buffer_frames": output_frames.value}
    except (AttributeError, OSError, TypeError) as error:
        return {"host_buffer_query_error": str(error)}


def summary(values):
    if not values:
        return None
    ordered = sorted(values)
    return {"min": round(ordered[0], 3), "median": round(statistics.median(ordered), 3),
            "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * .95))], 3)}


def probe_native(config):
    from app.services import monitor_worker
    from app.services.native_wasapi import TIMING_FIELDS, NativeWasapiStream
    stats = {}
    options = {"input_device_name": sd.query_devices(config["input"])["name"],
               "output_device_name": sd.query_devices(config["output"])["name"],
               "blocksize": config["blocksize"]}
    if config.get("raw"):
        # Exercise the production native bypass without ever making captured
        # microphone samples audible during an automated probe.
        options["gain"] = 0.0
    if config.get("input_exclusive"):
        options["input_exclusive"] = True
    stream = None
    previous_params = monitor_worker._live_params
    try:
        stream = NativeWasapiStream(options, stats)
        if config.get("effects"):
            monitor_worker._live_params = {
                **previous_params,
                **{name: float(value) for name, value in config["effects"].items()},
            }
        dsp = monitor_worker._audio_callback(2.0, stream.info.sample_rate, stats) if config.get("dsp") else None
        def callback(source, output, frames, clock, status):
            if dsp is not None:
                dsp(source, output, frames, clock, status)
            output.fill(0)  # Never play or persist microphone samples.
        stream.start(callback)
        if config.get("raw"):
            stream.set_raw(True)
        started, previous = time.monotonic(), -1
        rows = []
        while time.monotonic() - started < config["duration"]:
            stream.pump()
            if time.monotonic() - started > .2 and stats["rendered_frames"] != previous and len(rows) < 10000:
                rows.append(dict(stats))
                previous = stats["rendered_frames"]
        names = (*TIMING_FIELDS, "stream_latency_ms", "dsp_compute_ms", "effect_latency_ms",
                 "resample_ratio", "queue_ms")
        return {**stream.diagnostics(), "statistics": stats,
                "timings": {name: summary([row[name] for row in rows if row.get(name) is not None]) for name in names},
                "round_trip_latency_ms": None}
    finally:
        monitor_worker._live_params = previous_params
        if stream is not None:
            stream.close()


def probe(config):
    if config["kind"] == "native":
        return probe_native(config)
    from app.services import monitor_worker
    previous_params = monitor_worker._live_params
    try:
        if config.get("effects"):
            monitor_worker._live_params = {
                **previous_params,
                **{name: float(value) for name, value in config["effects"].items()},
                "dry_monitor": 0.0,
                "local_monitoring_enabled": 1.0,
            }
        return _probe_portaudio(config)
    finally:
        monitor_worker._live_params = previous_params


def _probe_portaudio(config):
    frames_seen, capture_age, render_lead = [], [], []
    glitches = 0
    started = time.monotonic()
    stats, restart = {}, threading.Event()
    dsp = None
    if config.get("dsp"):
        from app.services.monitor_worker import _audio_callback
        dsp = _audio_callback(2.0, config["rate"], stats)

    def observe(frames, clocks, status, has_input, has_output):
        nonlocal glitches
        # Ignore startup priming, keep bounded statistics only (never sample data).
        if time.monotonic() - started < .2:
            return
        glitches += bool(status)
        if len(frames_seen) >= 10000:
            return
        frames_seen.append(frames)
        if has_input and clocks.inputBufferAdcTime:
            capture_age.append((clocks.currentTime - clocks.inputBufferAdcTime) * 1000)
        if has_output and clocks.outputBufferDacTime:
            render_lead.append((clocks.outputBufferDacTime - clocks.currentTime) * 1000)

    def duplex_callback(_input, output, frames, clocks, status):
        if dsp is not None:
            dsp(_input, output, frames, clocks, status)
        output.fill(0)
        observe(frames, clocks, status, True, True)

    def input_callback(_input, frames, clocks, status):
        observe(frames, clocks, status, True, False)

    def output_callback(output, frames, clocks, status):
        output.fill(0)
        observe(frames, clocks, status, False, True)

    common = dict(samplerate=config["rate"], blocksize=config["blocksize"],
                  latency=config["latency"], dtype="float32")
    exclusive = config["mode"] == "exclusive"
    extra = (
        None if config["mode"] == "plain"
        else sd.WasapiSettings(exclusive=exclusive, auto_convert=not exclusive)
    )
    kind = config["kind"]
    if kind == "split":
        stream = WasapiMonitorStream(sd, {**common, "device": (config["input"], config["output"]),
                                     "channels": (1, 2), "extra_settings": (extra, extra)},
                                     duplex_callback, stats, restart)
    elif kind == "duplex":
        duplex_options = dict(
            common, device=(config["input"], config["output"]),
            channels=(1, 2), callback=duplex_callback,
        )
        if extra is not None:
            duplex_options["extra_settings"] = (extra, extra)
        stream = sd.Stream(**duplex_options)
    elif kind == "input":
        input_options = dict(
            common, device=config["input"], channels=1, callback=input_callback,
        )
        if extra is not None:
            input_options["extra_settings"] = extra
        stream = sd.InputStream(**input_options)
    else:
        output_options = dict(
            common, device=config["output"], channels=2, callback=output_callback,
        )
        if extra is not None:
            output_options["extra_settings"] = extra
        stream = sd.OutputStream(**output_options)
    try:
        stream.start()
        buffers = ({"input_host_buffer_frames": host_buffer_frames(stream.input).get("input_host_buffer_frames"),
                    "output_host_buffer_frames": host_buffer_frames(stream.output).get("output_host_buffer_frames")}
                   if kind == "split" else host_buffer_frames(stream))
        diagnostics = {"reported_latency_seconds": stream.latency, **buffers}
        time.sleep(config["duration"])
        diagnostics.update(callbacks=len(frames_seen), callback_frames=sorted(set(frames_seen)),
                           glitches=glitches, capture_age_ms=summary(capture_age), render_lead_ms=summary(render_lead))
        if kind == "split":
            diagnostics.update(**stats, restart_requested=restart.is_set())
        return diagnostics
    finally:
        try:
            stream.abort()
        finally:
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help=argparse.SUPPRESS)
    parser.add_argument("--duration", type=float, default=1.0)
    parser.add_argument("--split-only", action="store_true")
    parser.add_argument("--native-only", action="store_true", help="Probe only the current native shared monitor")
    parser.add_argument("--input-exclusive", action="store_true",
                        help="With --native-only, probe production exclusive-capture/shared-render mode")
    parser.add_argument(
        "--buffer-size", type=int,
        choices=(16, 32, 64, 128, 256, 512, 1024, 2048), default=16,
    )
    parser.add_argument("--dsp", action="store_true", help="Run normal microphone DSP, still output only silence")
    parser.add_argument("--effects-stress", action="store_true",
                        help="Run every microphone effect at a demanding setting; output remains silent")
    parser.add_argument("--raw", action="store_true", help="Exercise native dry bypass at zero gain")
    args = parser.parse_args()
    if args.case:
        config = json.loads(args.case)
        try:
            result = probe(config)
        except Exception as error:
            result = {"error": str(error)}
        print(json.dumps({**config, **result}), flush=True)
        return
    host = next((item for item in sd.query_hostapis() if item["name"] == "Windows WASAPI"), None)
    if host is None:
        raise SystemExit("Windows WASAPI unavailable")
    input_id, output_id = host["default_input_device"], host["default_output_device"]
    input_info, output_info = sd.query_devices(input_id), sd.query_devices(output_id)
    print(json.dumps({"input": input_info, "output": output_info,
                      "portaudio": sd.get_portaudio_version(), "sounddevice": sd.__version__,
                      "note": "Silence output; no sample recording. Timestamps are not acoustic loopback measurements."}), flush=True)
    rate = int(output_info["default_samplerate"])
    for mode in (("shared",) if args.native_only else ("exclusive",) if args.split_only else ("shared", "exclusive")):
        for kind in (("native",) if args.native_only else ("split",) if args.split_only else ("duplex", "input", "output")):
            for blocksize in ((args.buffer_size,) if args.native_only else (128, 256) if args.split_only else (128, 0)):
                effects = ({
                    "volume": 2.0, "reverb": 1.0, "echo": 1.0, "delay": 1.0,
                    "noise_suppression": 1.0, "octave": -0.5,
                } if args.effects_stress else None)
                config = dict(input=input_id, output=output_id, rate=rate, mode=mode, kind=kind,
                              blocksize=blocksize, latency=128 / rate, duration=max(.3, min(args.duration, 30)),
                              dsp=args.dsp or args.effects_stress, raw=args.raw,
                              input_exclusive=args.input_exclusive, effects=effects)
                try:
                    result = subprocess.run([sys.executable, __file__, "--case", json.dumps(config)],
                                            capture_output=True, text=True, timeout=config["duration"] + 12,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    print(result.stdout.strip() or json.dumps({**config, "error": result.stderr[-1000:]}), flush=True)
                except subprocess.TimeoutExpired:
                    print(json.dumps({**config, "error": "Driver probe timed out; subprocess terminated"}), flush=True)


if __name__ == "__main__":
    main()
