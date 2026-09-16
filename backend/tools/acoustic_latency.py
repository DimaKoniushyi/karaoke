"""Short acoustic loopback check; never persists captured microphone samples."""

import argparse
import json
import math
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def detect_pulse(pulse, capture, sample_rate, output_time, capture_start_time,
                 *, maximum_delay_ms=250, minimum_correlation=.35):
    """Locate one emitted code in the microphone signal, or reject a weak match."""
    pulse = np.asarray(pulse, dtype=np.float32).reshape(-1)
    capture = np.asarray(capture, dtype=np.float32).reshape(-1)
    if not sample_rate or not len(pulse) or len(capture) < len(pulse):
        return None
    first = max(0, math.floor((output_time - capture_start_time) * sample_rate))
    last = min(len(capture) - len(pulse),
               math.ceil((output_time - capture_start_time + maximum_delay_ms / 1000) * sample_rate))
    if first > last:
        return None
    region = capture[first:last + len(pulse)]
    dot = np.correlate(region, pulse, mode="valid")
    squares = np.square(region.astype(np.float64))
    energy = np.concatenate(([0.0], np.cumsum(squares)))
    window_energy = np.maximum(energy[len(pulse):] - energy[:-len(pulse)], 0.0)
    norm = np.sqrt(window_energy * float(np.dot(pulse, pulse)))
    scores = np.divide(dot, norm, out=np.zeros_like(dot), where=norm > 1e-12)
    # The speaker-to-microphone acoustic path may invert polarity. A negative
    # match is the same coded pulse, not evidence that the echo is absent.
    peak = int(np.argmax(np.abs(scores)))
    strength = float(abs(scores[peak]))
    if strength < minimum_correlation:
        return None
    heard_at = capture_start_time + (first + peak) / sample_rate
    latency_ms = (heard_at - output_time) * 1000
    if latency_ms < 0 or latency_ms > maximum_delay_ms:
        return None
    return {"latency_ms": round(latency_ms, 3), "correlation": round(strength, 3)}


def consistent_echoes(values, *, maximum_spread_ms=8):
    """Reject an apparent result when even one coded pulse disagrees."""
    ordered = sorted(float(value) for value in values)
    if len(ordered) < 3 or ordered[-1] - ordered[0] > maximum_spread_ms:
        return []
    return ordered


def probe_positions(sample_rate, duration):
    """Keep measuring across a long run, not just during its first second."""
    count = max(3, int(math.floor((duration - .2 - .3) / .4)) + 1)
    return [int(sample_rate * (.3 + .4 * index)) for index in range(count)]


def make_probe_pulse():
    rng = np.random.default_rng(82019)
    pulse = rng.choice([-1.0, 1.0], 256).astype(np.float32)
    return pulse * np.hanning(len(pulse)).astype(np.float32) * .24


def run_probe(input_device, output_device, *, duration=1.8,
              transport="wasapi-shared", blocksize=0):
    input_info, output_info = sd.query_devices(input_device), sd.query_devices(output_device)
    rate = int(output_info["default_samplerate"])
    if abs(input_info["default_samplerate"] - rate) > 1:
        raise RuntimeError("Input and output default sample rates differ; acoustic check unavailable")
    pulse = make_probe_pulse()
    positions = [int(rate * second) for second in (.3, .7, 1.1)]
    emitted = {}
    captured = []
    capture_start = [None]
    output_frame = [0]
    errors = []

    def callback(indata, outdata, frames, timing, status):
        if status:
            errors.append(str(status))
        if capture_start[0] is None:
            capture_start[0] = float(timing.inputBufferAdcTime)
        captured.append(indata[:, 0].copy())
        outdata.fill(0)
        for ordinal, position in enumerate(positions):
            begin = max(position - output_frame[0], 0)
            end = min(position + len(pulse) - output_frame[0], frames)
            if begin < end:
                offset = max(output_frame[0] - position, 0)
                outdata[begin:end, :] = pulse[offset:offset + end - begin, None]
                if ordinal not in emitted:
                    emitted[ordinal] = float(timing.outputBufferDacTime) + begin / rate
        output_frame[0] += frames

    # A temporary duplex stream probes the device path. PortAudio's ADC/DAC
    # timestamps can omit hidden buffering; only the two-echo monitor probe
    # below measures return latency on one microphone sample clock.
    stream_options = {
        "device": (input_device, output_device), "samplerate": rate,
        "channels": (1, min(2, output_info["max_output_channels"])),
        "dtype": "float32", "blocksize": blocksize, "callback": callback,
    }
    if transport in ("wasapi-shared", "wasapi-hybrid", "wasapi-exclusive"):
        stream_options["extra_settings"] = (
            sd.WasapiSettings(exclusive=transport != "wasapi-shared"),
            sd.WasapiSettings(exclusive=transport == "wasapi-exclusive"),
        )
    elif transport != "wdm-ks":
        raise ValueError(f"Unsupported acoustic probe transport: {transport}")
    with sd.Stream(**stream_options):
        time.sleep(duration)
    if errors or not captured or capture_start[0] is None:
        return {"status": "unavailable", "reason": "audio_stream_glitch", "errors": errors[:3]}
    microphone = np.concatenate(captured)
    timeline_matches = [detect_pulse(pulse, microphone, rate, position / rate, 0)
                        for position in positions]
    timeline_delays = [match["latency_ms"] for match in timeline_matches if match is not None]
    timeline_ms = (round(float(np.median(timeline_delays)), 3)
                   if len(timeline_delays) >= 2 and max(timeline_delays) - min(timeline_delays) <= 8
                   else None)
    matches = [detect_pulse(pulse, microphone, rate, emitted[index], capture_start[0])
               for index in range(len(positions)) if index in emitted]
    good = [match for match in matches if match is not None]
    if len(good) < 2:
        return {"status": "unavailable", "reason": "pulse_not_reliably_detected",
                "detected": len(good), "emitted": len(emitted)}
    delays = np.array([match["latency_ms"] for match in good])
    if float(np.max(delays) - np.min(delays)) > 8:
        return {"status": "unavailable", "reason": "inconsistent_pulse_delays",
                "detected": len(good)}
    return {"status": "measured", "reported_clock_roundtrip_ms": round(float(np.median(delays)), 3),
            "detected": len(good), "reported_clock_delays_ms": [match["latency_ms"] for match in good],
            "pulse_correlations": [match["correlation"] for match in good],
            "sample_timeline_roundtrip_ms": timeline_ms,
            "sample_rate": rate,
            "input_device": input_info["name"], "output_device": output_info["name"],
            "transport": f"temporary-{transport}",
            "note": "Clock estimate and duplex sample offset; not a two-echo monitor measurement"}


def run_split_exclusive_probe(input_device, output_device, *, duration=5.0,
                              blocksize=128):
    """Measure one physical speaker-to-microphone return with split exclusive I/O.

    Both endpoints are event driven. Output is a quiet coded pulse, never
    captured speech; microphone samples exist only in process memory.
    """
    from app.services.wasapi_monitor_stream import WasapiMonitorStream

    input_info, output_info = sd.query_devices(input_device), sd.query_devices(output_device)
    rate = int(output_info["default_samplerate"])
    if abs(input_info["default_samplerate"] - rate) > 1:
        raise RuntimeError("Input and output default sample rates differ")
    pulse = make_probe_pulse()
    positions = probe_positions(rate, duration)
    captured, errors = [], []
    current_frame = [0]
    statistics, failed = {}, threading.Event()

    def callback(indata, outdata, frames, _timing, status):
        if status:
            errors.append(str(status))
        captured.append(indata[:, 0].copy())
        outdata.fill(0)
        for position in positions:
            begin = max(position - current_frame[0], 0)
            end = min(position + len(pulse) - current_frame[0], frames)
            if begin < end:
                offset = max(current_frame[0] - position, 0)
                outdata[begin:end, :] = pulse[offset:offset + end - begin, None]
        current_frame[0] += frames

    options = {
        "device": (input_device, output_device), "samplerate": rate,
        "channels": (1, min(2, output_info["max_output_channels"])),
        "dtype": "float32", "blocksize": blocksize,
        "latency": blocksize / rate,
        "extra_settings": (sd.WasapiSettings(exclusive=True),
                           sd.WasapiSettings(exclusive=True)),
    }
    stream = WasapiMonitorStream(sd, options, callback, statistics, failed)
    try:
        stream.start()
        time.sleep(duration)
    finally:
        stream.abort()
        stream.close()
    activity = {
        "queue_underruns_after_start": statistics.get("queue_underruns_after_start"),
        "queue_dropped_frames": statistics.get("queue_dropped_frames"),
        "glitch_count": statistics.get("glitch_count", 0),
        "callback_errors": errors[:3],
        "callback_failed": failed.is_set(),
    }
    if failed.is_set() or errors or not captured:
        return {"status": "unavailable", "reason": "audio_stream_glitch", **activity}
    microphone = np.concatenate(captured)
    matches = [detect_pulse(pulse, microphone, rate, position / rate, 0)
               for position in positions]
    detected_indices = [index for index, match in enumerate(matches) if match is not None]
    good = [match for match in matches if match is not None]
    delays = [match["latency_ms"] for match in good]
    consistent = consistent_echoes(delays)
    if len(good) < math.ceil(len(positions) * .75) or not consistent:
        return {"status": "unavailable", "reason": "pulse_not_reliably_detected",
                "detected": len(good), "emitted": len(positions),
                "pulse_delays_ms": delays, "detected_indices": detected_indices,
                "pulse_correlations": [match["correlation"] for match in good], **activity}
    return {"status": "measured", "acoustic_roundtrip_ms": round(float(np.median(consistent)), 3),
            "pulse_delays_ms": delays, "detected": len(good), "emitted": len(positions),
            "detected_indices": detected_indices,
            "pulse_correlations": [match["correlation"] for match in good],
            "sample_rate": rate, "input_device": input_info["name"],
            "output_device": output_info["name"],
            "transport": "wasapi-split-exclusive", **activity}


def run_split_exclusive_monitor_loopback(input_device, output_device, *,
                                         duration=10.0, blocksize=128,
                                         baseline=None):
    """Compare alternating monitor-on/off returns on one microphone timeline."""
    from app.services.wasapi_monitor_stream import WasapiMonitorStream

    if baseline is None:
        baseline = run_split_exclusive_probe(
            input_device, output_device, duration=min(duration, 10.0), blocksize=blocksize,
        )
    if baseline.get("status") != "measured":
        return {"status": "unavailable", "reason": "device_roundtrip_unavailable",
                "baseline": baseline}
    rate = baseline["sample_rate"]
    pulse = make_probe_pulse()
    positions = probe_positions(rate, duration)
    first_window = int(round((baseline["acoustic_roundtrip_ms"] - 6) * rate / 1000))
    last_window = int(round((baseline["acoustic_roundtrip_ms"] + 12) * rate / 1000))
    captured, errors = [], []
    current_frame = [0]
    statistics, failed = {}, threading.Event()

    def callback(indata, outdata, frames, _timing, status):
        if status:
            errors.append(str(status))
        captured.append(indata[:, 0].copy())
        outdata.fill(0)
        for ordinal, position in enumerate(positions):
            begin = max(position - current_frame[0], 0)
            end = min(position + len(pulse) - current_frame[0], frames)
            if begin < end:
                offset = max(current_frame[0] - position, 0)
                outdata[begin:end, :] = pulse[offset:offset + end - begin, None]
            # Odd pulses are controls: acoustic reflections remain, while the
            # software monitor return is deliberately absent.
            if ordinal % 2:
                continue
            gate_begin = max(position + first_window - current_frame[0], 0)
            gate_end = min(position + last_window - current_frame[0], frames)
            if gate_begin < gate_end:
                outdata[gate_begin:gate_end, :] += np.clip(
                    indata[gate_begin:gate_end, :1] * 2, -.25, .25
                )
        current_frame[0] += frames

    options = {
        "device": (input_device, output_device), "samplerate": rate,
        "channels": (1, min(2, sd.query_devices(output_device)["max_output_channels"])),
        "dtype": "float32", "blocksize": blocksize,
        "latency": blocksize / rate,
        "extra_settings": (sd.WasapiSettings(exclusive=True),
                           sd.WasapiSettings(exclusive=True)),
    }
    stream = WasapiMonitorStream(sd, options, callback, statistics, failed)
    try:
        stream.start()
        time.sleep(duration)
    finally:
        stream.abort()
        stream.close()
    activity = {
        "queue_underruns_after_start": statistics.get("queue_underruns_after_start"),
        "queue_dropped_frames": statistics.get("queue_dropped_frames"),
        "glitch_count": statistics.get("glitch_count", 0),
        "callback_errors": errors[:3], "callback_failed": failed.is_set(),
    }
    if failed.is_set() or errors or not captured:
        return {"status": "unavailable", "reason": "audio_stream_glitch",
                "baseline": baseline, **activity}
    microphone = np.concatenate(captured)
    on_delays, off_delays, first_count = [], [], 0
    for ordinal, position in enumerate(positions):
        first = detect_pulse(pulse, microphone, rate, position / rate, 0,
                             maximum_delay_ms=baseline["acoustic_roundtrip_ms"] + 15)
        if first is None:
            continue
        first_count += 1
        first_at = position / rate + first["latency_ms"] / 1000
        second = detect_pulse(pulse, microphone, rate, first_at + .008, 0,
                              maximum_delay_ms=80, minimum_correlation=.2)
        if second is not None:
            (off_delays if ordinal % 2 else on_delays).append(
                round(8 + second["latency_ms"], 3)
            )
    required_on = math.ceil(math.ceil(len(positions) / 2) * .75)
    consistent_on = consistent_echoes(on_delays)
    consistent_off = consistent_echoes(off_delays)
    result = {"baseline": baseline, "monitor_on_delays_ms": on_delays,
              "monitor_off_delays_ms": off_delays, "first_echoes": first_count,
              "emitted": len(positions), **activity}
    if len(consistent_on) < required_on:
        return {"status": "unavailable", "reason": "monitor_echo_not_reliably_detected",
                **result}
    candidate = float(np.median(consistent_on))
    if consistent_off and abs(float(np.median(consistent_off)) - candidate) < 8:
        return {"status": "unavailable", "reason": "echo_matches_room_reflection",
                **result}
    return {"status": "measured", "acoustic_monitor_ms": round(candidate, 3),
            "transport": "wasapi-split-exclusive", **result}


def run_portaudio_monitor_loopback(input_device, output_device, *,
                                   transport="wasapi-exclusive", duration=1.8,
                                   blocksize=0, effects=None):
    """Measure monitor return using two echoes on one microphone sample clock."""
    baseline = run_probe(input_device, output_device,
                         duration=duration, transport=transport,
                         blocksize=blocksize)
    baseline_ms = baseline.get("sample_timeline_roundtrip_ms")
    if baseline.get("status") != "measured" or baseline_ms is None:
        return {"status": "unavailable", "reason": "device_roundtrip_unavailable",
                "baseline": baseline}
    input_info, output_info = sd.query_devices(input_device), sd.query_devices(output_device)
    rate = int(output_info["default_samplerate"])
    pulse = make_probe_pulse()
    positions = probe_positions(rate, duration)
    first_window = int(round((baseline_ms - 8) * rate / 1000))
    last_window = int(round((baseline_ms + 24) * rate / 1000))
    captured, errors = [], []
    position = [0]
    callback_count = [0]
    from app.services import monitor_worker
    previous_params = monitor_worker._live_params
    if effects is not None:
        monitor_worker._live_params = {
            **previous_params, "volume": 1.0, "dry_monitor": 0.0,
            "local_monitoring_enabled": 1.0,
            **{key: float(value) for key, value in effects.items()},
        }
        process_effects = monitor_worker._audio_callback(1.0, rate, {})
    else:
        process_effects = None

    def callback(indata, outdata, frames, _timing, status):
        callback_count[0] += 1
        if status:
            errors.append(str(status))
        captured.append(indata[:, 0].copy())
        outdata.fill(0)
        if process_effects is not None:
            processed = np.zeros((frames, 1), dtype=np.float32)
            process_effects(indata, processed, frames, _timing, status)
            monitor_source = processed[:, 0]
        else:
            monitor_source = indata[:, 0]
        for start in positions:
            pulse_begin = max(start - position[0], 0)
            pulse_end = min(start + len(pulse) - position[0], frames)
            if pulse_begin < pulse_end:
                offset = max(position[0] - start, 0)
                outdata[pulse_begin:pulse_end, :] = pulse[offset:offset + pulse_end - pulse_begin, None]
            gate_begin = max(start + first_window - position[0], 0)
            gate_end = min(start + last_window - position[0], frames)
            if gate_begin < gate_end:
                outdata[gate_begin:gate_end, :] += np.clip(
                    monitor_source[gate_begin:gate_end, None] * 4, -.25, .25)
        position[0] += frames

    options = {
        "device": (input_device, output_device), "samplerate": rate,
        "channels": (1, min(2, output_info["max_output_channels"])),
        "dtype": "float32", "blocksize": blocksize, "callback": callback,
    }
    if transport.startswith("wasapi-"):
        options["extra_settings"] = (
            sd.WasapiSettings(exclusive=transport != "wasapi-shared"),
            sd.WasapiSettings(exclusive=transport == "wasapi-exclusive"),
        )
    try:
        with sd.Stream(**options):
            time.sleep(duration)
    finally:
        monitor_worker._live_params = previous_params
    if errors or not captured:
        return {"status": "unavailable", "reason": "audio_stream_glitch",
                "errors": errors[:3], "baseline": baseline}
    microphone = np.concatenate(captured)
    delays = []
    for start in positions:
        first = detect_pulse(pulse, microphone, rate, start / rate, 0,
                             maximum_delay_ms=baseline_ms + 10)
        if first is None:
            continue
        second_start = (start / rate + first["latency_ms"] / 1000) + .008
        second = detect_pulse(pulse, microphone, rate, second_start, 0,
                              maximum_delay_ms=80, minimum_correlation=.2)
        if second is not None:
            delays.append(round(8 + second["latency_ms"], 3))
    consistent = consistent_echoes(delays)
    if not consistent or len(delays) < max(3, math.ceil(len(positions) * .75)):
        return {"status": "unavailable", "reason": "second_echo_not_reliably_detected",
                "monitor_delays_ms": delays, "emitted": len(positions),
                "detected": len(delays), "baseline": baseline}
    return {"status": "measured", "acoustic_monitor_ms": round(float(np.median(consistent)), 3),
            "monitor_delays_ms": delays, "baseline": baseline,
            "transport": transport, "effects": effects,
            "callback_count": callback_count[0],
            "emitted": len(positions), "detected": len(delays),
            "max_acoustic_monitor_ms": max(delays)}


def run_native_shared_probe(input_name, output_name, *, duration=1.8,
                            input_exclusive=False, blocksize=16):
    """Measure the acoustic return through the native engine, shared by default."""
    from app.services.native_wasapi import NativeWasapiStream

    statistics = {}
    stream = NativeWasapiStream({
        "input_device_name": input_name,
        "output_device_name": output_name,
        "blocksize": blocksize,
        "gain": 1.0,
        "input_exclusive": input_exclusive,
    }, statistics)
    try:
        rate = stream.info.sample_rate
        pulse = make_probe_pulse()
        positions = probe_positions(rate, duration)
        captured = []
        processed_frames = [0]

        def callback(source, output, frames, _clock, _status):
            captured.append(source[:, 0].copy())
            output.fill(0)
            for position in positions:
                begin = max(position - processed_frames[0], 0)
                end = min(position + len(pulse) - processed_frames[0], frames)
                if begin < end:
                    offset = max(processed_frames[0] - position, 0)
                    output[begin:end, 0] = pulse[offset:offset + end - begin]
            processed_frames[0] += frames

        stream.start(callback)
        started = time.monotonic()
        stream_latencies = []
        output_leads = []
        queue_times = []
        while time.monotonic() - started < duration:
            stream.pump()
            current_latency = statistics.get("stream_latency_ms")
            if isinstance(current_latency, (int, float)) and math.isfinite(current_latency) and current_latency > 0:
                stream_latencies.append(current_latency)
            for field, samples in (("output_clock_lead_ms", output_leads),
                                   ("queue_ms", queue_times)):
                value = statistics.get(field)
                if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
                    samples.append(value)
        if not captured:
            return {"status": "unavailable", "reason": "no_microphone_samples"}
        microphone = np.concatenate(captured)
        diagnostics = stream.diagnostics() if hasattr(stream, "diagnostics") else {}
        activity = {
            "captured_frames": statistics.get("captured_frames", 0),
            "rendered_frames": statistics.get("rendered_frames", 0),
            "queue_dropped_frames": statistics.get("queue_dropped_frames", 0),
            "queue_underruns": statistics.get("queue_underruns", 0),
            "glitch_count": statistics.get("glitch_count", 0),
            "queue_ms": statistics.get("queue_ms"),
            "queue_p10_ms": round(float(np.percentile(queue_times, 10)), 3) if queue_times else None,
            "queue_p90_ms": round(float(np.percentile(queue_times, 90)), 3) if queue_times else None,
            "output_clock_lead_ms": statistics.get("output_clock_lead_ms"),
            "output_clock_lead_p10_ms": round(float(np.percentile(output_leads, 10)), 3) if output_leads else None,
            "output_clock_lead_p90_ms": round(float(np.percentile(output_leads, 90)), 3) if output_leads else None,
            "program_residence_ms": statistics.get("program_residence_ms"),
            "stream_latency_ms": statistics.get("stream_latency_ms"),
            "stream_latency_p50_ms": (round(float(np.median(stream_latencies)), 3)
                                      if stream_latencies else None),
            "stream_latency_p10_ms": (round(float(np.percentile(stream_latencies, 10)), 3)
                                      if stream_latencies else None),
            "stream_latency_p90_ms": (round(float(np.percentile(stream_latencies, 90)), 3)
                                      if stream_latencies else None),
            "microphone_peak": round(float(np.max(np.abs(microphone))), 4),
        }
        activity.update({key: diagnostics.get(key) for key in (
            "minimum_period_latency_ms", "negotiated_period_latency_ms",
            "input_period_frames", "output_period_frames", "latency_limit",
            "input_exclusive", "output_exclusive", "input_raw", "output_raw",
        )})
        matches = [detect_pulse(pulse, microphone, rate, position / rate, 0)
                   for position in positions]
        good = [match for match in matches if match is not None]
        if len(good) < 2:
            return {"status": "unavailable", "reason": "pulse_not_reliably_detected",
                    "detected": len(good), "emitted": len(positions), **activity}
        delays = np.array([match["latency_ms"] for match in good])
        if float(np.max(delays) - np.min(delays)) > 8:
            return {"status": "unavailable", "reason": "inconsistent_pulse_delays",
                    "detected": len(good),
                    "pulse_delays_ms": [match["latency_ms"] for match in good],
                    **activity}
        acoustic_ms = round(float(np.median(delays)), 3)
        stream_ms = activity["stream_latency_p50_ms"]
        unreported_ms = (round(acoustic_ms - stream_ms, 3)
                         if isinstance(stream_ms, (int, float)) and math.isfinite(stream_ms)
                         and stream_ms > 0 else None)
        return {"status": "measured", "acoustic_roundtrip_ms": acoustic_ms,
                "unreported_latency_ms": unreported_ms,
                "detected": len(good), "pulse_delays_ms": [match["latency_ms"] for match in good],
                "pulse_correlations": [match["correlation"] for match in good],
                "sample_rate": rate, "input_device": input_name, "output_device": output_name,
                "transport": ("native-wasapi-hybrid-test" if input_exclusive
                              else "native-wasapi-fully-shared-test"), **activity}
    finally:
        stream.close()


def run_native_monitor_loopback(input_name, output_name, *, duration=1.8,
                                effects=None, input_exclusive=False,
                                blocksize=16, alternate_controls=False):
    """Measure a dry microphone-monitor echo, not just speaker-to-mic return.

    The first, low-level code reaches the microphone from the speaker. Only
    that first return is copied to the shared renderer for a short gated
    window, so it can be heard a second time without sustained feedback.
    The interval between the two captured codes is the monitor path.
    """
    from app.services import monitor_worker
    from app.services.native_wasapi import NativeWasapiStream

    baseline = run_native_shared_probe(
        input_name, output_name, duration=duration,
        input_exclusive=input_exclusive,
        blocksize=blocksize,
    )
    if baseline.get("status") != "measured":
        return {"status": "unavailable", "reason": "device_roundtrip_unavailable",
                "baseline": baseline}
    baseline_ms = float(baseline["acoustic_roundtrip_ms"])
    if not 5 <= baseline_ms <= 120:
        return {"status": "unavailable", "reason": "device_roundtrip_out_of_range",
                "baseline": baseline}
    statistics = {}
    stream = NativeWasapiStream({"input_device_name": input_name,
                                "output_device_name": output_name,
                                "blocksize": blocksize,
                                "input_exclusive": input_exclusive}, statistics)
    previous_params = monitor_worker._live_params
    try:
        rate = int(stream.info.sample_rate)
        if effects is not None:
            monitor_worker._live_params = {
                **previous_params, "volume": 1.0, "dry_monitor": 0.0,
                "local_monitoring_enabled": 1.0,
                **{key: float(value) for key, value in effects.items()},
            }
            process_effects = monitor_worker._audio_callback(1.0, rate, statistics)
        else:
            process_effects = None
        pulse = make_probe_pulse()
        positions = probe_positions(rate, duration)
        first_window = int(round((baseline_ms - 8) * rate / 1000))
        # Opening the second stream can shift a shared endpoint's event phase
        # by one 10-ms period. Keep the gate wide enough to pass that first
        # return, but still close it before the second echo can feed back.
        last_window = int(round((baseline_ms + 24) * rate / 1000))
        captured = []
        processed_frames = [0]

        def callback(source, output, frames, _clock, _status):
            captured.append(source[:, 0].copy())
            output.fill(0)
            if process_effects is not None:
                processed = np.zeros_like(output)
                process_effects(source, processed, frames, _clock, _status)
                monitor_source = processed[:, 0]
            else:
                monitor_source = source[:, 0]
            for ordinal, position in enumerate(positions):
                pulse_start = max(position - processed_frames[0], 0)
                pulse_end = min(position + len(pulse) - processed_frames[0], frames)
                if pulse_start < pulse_end:
                    offset = max(processed_frames[0] - position, 0)
                    output[pulse_start:pulse_end, 0] = pulse[offset:offset + pulse_end - pulse_start]
                if alternate_controls and ordinal % 2:
                    continue
                gate_start = max(position + first_window - processed_frames[0], 0)
                gate_end = min(position + last_window - processed_frames[0], frames)
                if gate_start < gate_end:
                    output[gate_start:gate_end, 0] += np.clip(
                        monitor_source[gate_start:gate_end] * 4, -.25, .25
                    )
            processed_frames[0] += frames

        stream.start(callback)
        started = time.monotonic()
        while time.monotonic() - started < duration:
            stream.pump()
        if not captured:
            return {"status": "unavailable", "reason": "no_microphone_samples"}
        if statistics.get("queue_dropped_frames", 0):
            return {"status": "unavailable", "reason": "audio_frames_dropped",
                    "queue_dropped_frames": statistics["queue_dropped_frames"],
                    "queue_underruns": statistics.get("queue_underruns", 0),
                    "glitch_count": statistics.get("glitch_count", 0)}
        microphone = np.concatenate(captured)
        first_delays, monitor_delays, control_delays, correlations = [], [], [], []
        first_detected = 0
        for ordinal, position in enumerate(positions):
            first = detect_pulse(
                pulse, microphone, rate, position / rate, 0,
                maximum_delay_ms=baseline_ms + 20,
            )
            if first is None:
                continue
            first_detected += 1
            first_at = position / rate + first["latency_ms"] / 1000
            second_search = first_at + .008
            second = detect_pulse(pulse, microphone, rate, second_search, 0,
                                  maximum_delay_ms=80,
                                  minimum_correlation=.2)
            if second is None:
                continue
            delay = round(8 + second["latency_ms"], 3)
            if alternate_controls and ordinal % 2:
                control_delays.append(delay)
            else:
                first_delays.append(first["latency_ms"])
                monitor_delays.append(delay)
                correlations.append(second["correlation"])
        required = (math.ceil(math.ceil(len(positions) / 2) * .75)
                    if alternate_controls else 2)
        if len(monitor_delays) < required:
            return {"status": "unavailable", "reason": "second_echo_not_reliably_detected",
                    "detected": len(monitor_delays), "first_detected": first_detected,
                    "monitor_off_delays_ms": control_delays,
                    "acoustic_device_roundtrip_ms": baseline_ms,
                    "microphone_peak": round(float(np.max(np.abs(microphone))), 4),
                    "queue_dropped_frames": statistics.get("queue_dropped_frames"),
                    "queue_underruns": statistics.get("queue_underruns", 0),
                    "glitch_count": statistics.get("glitch_count", 0)}
        consistent = consistent_echoes(monitor_delays)
        if not consistent:
            return {"status": "unavailable", "reason": "inconsistent_monitor_echo",
                    "monitor_delays_ms": monitor_delays,
                    "monitor_off_delays_ms": control_delays}
        if alternate_controls:
            control_cluster = consistent_echoes(control_delays)
            if control_cluster and abs(float(np.median(control_cluster))
                                       - float(np.median(consistent))) < 8:
                return {"status": "unavailable", "reason": "echo_matches_room_reflection",
                        "monitor_delays_ms": monitor_delays,
                        "monitor_off_delays_ms": control_delays}
        return {"status": "measured",
                "acoustic_monitor_ms": round(float(np.median(consistent)), 3),
                "acoustic_device_roundtrip_ms": baseline_ms,
                "first_echo_delays_ms": first_delays,
                "monitor_delays_ms": monitor_delays,
                "monitor_off_delays_ms": control_delays,
                "consistent_echo_count": len(consistent),
                "second_echo_correlations": correlations,
                "sample_rate": rate, "input_device": input_name,
                "output_device": output_name,
                "effects": effects,
                "effect_latency_ms": statistics.get("effect_latency_ms"),
                "input_exclusive": input_exclusive,
                "output_exclusive": False,
                "queue_dropped_frames": statistics.get("queue_dropped_frames"),
                "queue_underruns": statistics.get("queue_underruns", 0),
                "glitch_count": statistics.get("glitch_count", 0)}
    finally:
        monitor_worker._live_params = previous_params
        stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=int)
    parser.add_argument("--output", type=int)
    parser.add_argument("--duration", type=float, default=1.8,
                        help="Seconds to probe with repeated quiet pulses")
    parser.add_argument("--monitor-loopback", action="store_true",
                        help="Measure a gated two-echo dry monitor path; place mic near speaker")
    parser.add_argument("--effects-stress", action="store_true",
                        help="With --monitor-loopback, exercise the production DSP at maximum effects")
    parser.add_argument("--input-exclusive", action="store_true",
                        help="Diagnostic hybrid: exclusive microphone, shared output")
    args = parser.parse_args()
    host = next((api for api in sd.query_hostapis() if api["name"] == "Windows WASAPI"), None)
    if host is None:
        raise SystemExit("Windows WASAPI unavailable")
    input_device = args.input if args.input is not None else host["default_input_device"]
    output_device = args.output if args.output is not None else host["default_output_device"]
    try:
        input_name = sd.query_devices(input_device)["name"]
        output_name = sd.query_devices(output_device)["name"]
        effects = ({"octave": -.5, "reverb": 1.0, "echo": 1.0,
                    "delay": 1.0, "noise_suppression": 1.0}
                   if args.effects_stress else None)
        hybrid = {"input_exclusive": True} if args.input_exclusive else {}
        result = (run_native_monitor_loopback(
            input_name, output_name, effects=effects, duration=args.duration, **hybrid,
        ) if args.monitor_loopback else run_native_shared_probe(
            input_name, output_name, duration=args.duration, **hybrid,
        ))
    except Exception as error:
        result = {"status": "unavailable", "reason": str(error)}
    print("ACOUSTIC_LATENCY_DIAGNOSTICS " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
