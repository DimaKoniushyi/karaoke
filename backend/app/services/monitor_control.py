"""One coalescing hardware-command lane; no DB sessions cross thread boundaries."""

import json
import logging
import math
import threading
import time

logger = logging.getLogger(__name__)

_STREAM_STATISTICS = ("callback_frames", "callback_count", "glitch_count", "queue_frames",
                      "queue_capacity_frames", "queue_underruns", "queue_dropped_frames",
                      "queue_contentions", "queue_ms", "queue_capacity_ms", "queue_underruns_after_start",
                      "queue_wait_ms", "dsp_compute_ms", "effect_latency_ms", "stream_latency_ms", "real_latency_ms",
                      "capture_delivery_ms", "program_residence_ms", "queue_residence_ms", "output_clock_lead_ms",
                      "render_submit_ms", "render_padding_ms", "capture_processing_ms", "event_wait_ms", "pump_gap_ms",
                      "captured_frames", "rendered_frames", "resample_ratio")
_DIAGNOSTIC_COUNTERS = (
    "callback_count", "glitch_count", "queue_underruns", "queue_dropped_frames",
    "captured_frames", "rendered_frames",
)


class MonitorCancelled(RuntimeError):
    pass


class MonitorControl:
    def __init__(self, execution_lock=None):
        self.condition = threading.Condition(threading.RLock())
        self.execution = execution_lock if execution_lock is not None else threading.RLock()
        self.local = threading.local()
        self.token = threading.Event()
        self.pending = None
        self.live = None
        self.thread = None
        self.latency_breakdown_logged_at = 0.0
        self.real_latency_logged_at = 0.0
        self.monitor_diagnostics_logged_at = 0.0
        self.monitor_diagnostics_previous = {}
        self.status = {"state": "idle", "fallback_count": 0, "glitch_fallback_count": 0}

    def _begin(self, state, details):
        self.token.set()
        self.token = threading.Event()
        self.live = None
        self.monitor_diagnostics_logged_at = 0.0
        self.monitor_diagnostics_previous = {}
        self.status = {
            "state": state, "fallback_count": 0, "glitch_fallback_count": 0,
            "requested_at": time.monotonic(), **details,
            "request_id": str(time.monotonic_ns()),
        }
        return self.token

    def submit(self, action, *, state="starting", **details):
        with self.condition:
            token = self._begin(state, details)
            self.pending = (token, action)
            self._wake()

    def update_live(self, action):
        with self.condition:
            # A change during startup is applied after the new stream is ready.
            self.live = (self.token, action)
            self._wake()

    def _wake(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, name="monitor-control", daemon=True)
            self.thread.start()
        self.condition.notify_all()

    def cancel(self):
        with self.condition:
            self.token.set()
            self.pending = self.live = None
            self.status = {"state": "idle", "fallback_count": 0, "glitch_fallback_count": 0}

    def check(self):
        token = getattr(self.local, "token", None)
        if token is not None and token.is_set():
            raise MonitorCancelled("Monitoring request was superseded")

    def publish(self, token=None, **fields):
        token = token if token is not None else getattr(self.local, "token", None)
        with self.condition:
            if token is not None and (token is not self.token or token.is_set()):
                return
            self.status.update(fields)

    def snapshot(self):
        with self.condition:
            result = dict(self.status)
        started = result.pop("requested_at", None)
        if started is not None:
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000)
        return result

    def event(self, token, message):
        with self.condition:
            if token is not None and (token is not self.token or token.is_set()):
                return
            event = message.get("event")
            if event == "level":
                for key in _STREAM_STATISTICS:
                    if key in message:
                        self.status[key] = message[key]
                stream_latency = message.get("stream_latency_ms")
                now = time.monotonic()
                if (self.status.get("state") == "running" and
                        now - self.monitor_diagnostics_logged_at >= 5.0):
                    payload = dict(self.status)
                    requested_at = payload.pop("requested_at", None)
                    if requested_at is not None:
                        payload["elapsed_ms"] = round((now - requested_at) * 1000)
                    for key in _DIAGNOSTIC_COUNTERS:
                        current = payload.get(key)
                        previous = self.monitor_diagnostics_previous.get(key)
                        if isinstance(current, (int, float)) and not isinstance(current, bool):
                            payload[f"delta_{key}"] = (
                                current - previous
                                if isinstance(previous, (int, float)) and not isinstance(previous, bool)
                                else current
                            )
                    self.monitor_diagnostics_previous = {
                        key: payload[key] for key in _DIAGNOSTIC_COUNTERS if key in payload
                    }
                    logger.info(
                        "AUDIO_MONITOR_DIAGNOSTICS %s",
                        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    )
                    self.monitor_diagnostics_logged_at = now
                # Logged periodically (not just once) so a latency chase has an
                # ongoing trail to compare against, not a single early sample.
                if (str(self.status.get("engine", "")).startswith("wasapi-native-") and
                        isinstance(stream_latency, (int, float)) and math.isfinite(stream_latency) and
                        stream_latency > 0 and now - self.latency_breakdown_logged_at >= 5.0):
                    logger.info(
                        "WASAPI latency breakdown: stream_ms=%s capture_ms=%s program_ms=%s "
                        "queue_ms=%s output_lead_ms=%s padding_ms=%s dsp_ms=%s effect_ms=%s "
                        "input_period=%s@%s output_period=%s@%s",
                        stream_latency, message.get("capture_delivery_ms"), message.get("program_residence_ms"),
                        message.get("queue_residence_ms"), message.get("output_clock_lead_ms"),
                        message.get("render_padding_ms"), message.get("dsp_compute_ms"),
                        message.get("effect_latency_ms"),
                        self.status.get("input_period_frames"), self.status.get("sample_rate"),
                        self.status.get("output_period_frames"), self.status.get("output_sample_rate"),
                    )
                    self.latency_breakdown_logged_at = now
                # The real, hardware-timestamped mic-to-speaker round trip
                # (ADC capture time to DAC playback time, measured by the
                # driver itself) -- covers every engine, not just the native
                # WASAPI one, and reflects the full path through the DSP/
                # effects chain instead of a requested-buffer-size estimate.
                real_latency = message.get("real_latency_ms")
                if (isinstance(real_latency, (int, float)) and math.isfinite(real_latency) and
                        real_latency > 0 and now - self.real_latency_logged_at >= 5.0):
                    logger.info(
                        "Measured microphone-to-speaker latency: real_ms=%s engine=%s dsp_ms=%s",
                        round(real_latency, 3), self.status.get("engine"), message.get("dsp_compute_ms"),
                    )
                    self.real_latency_logged_at = now
            if event == "started" and "buffer_size" in message:
                # Normalize the existing ASIO bridge protocol without changing
                # its command, callback, buffer selection or effects.
                message = {**message, "blocksize": message["buffer_size"], "mode": "ASIO",
                           "latency_source": message.get("latency_source", "asio-driver-report")}
                rate = float(message.get("sample_rate") or 0)
                if math.isfinite(rate) and rate > 0:
                    for kind in ("input", "output"):
                        samples = message.get(f"{kind}_latency")
                        if isinstance(samples, (int, float)) and math.isfinite(samples) and samples >= 0:
                            message[f"{kind}_latency_ms"] = samples * 1000 / rate
            if event in {"started", "fallback"}:
                for key in ("blocksize", "sample_rate", "mode", "engine", "driver", "requested_driver", "failed_driver", "latency", "latency_source", "input_latency_ms", "output_latency_ms",
                            "input_device", "output_device", "requested_blocksize",
                            "output_sample_rate", "input_period_frames", "output_period_frames",
                            "input_raw", "output_raw", "input_exclusive", "output_exclusive",
                            "input_min_period_frames", "output_min_period_frames",
                            "input_period_locked", "output_period_locked", "minimum_period_latency_ms",
                            "negotiated_period_latency_ms", "latency_limit"):
                    if key in message:
                        self.status[key] = message[key]
                if event == "started":
                    self.latency_breakdown_logged_at = 0.0
                    self.real_latency_logged_at = 0.0
                    # A rejected automatic transport can emit an error before
                    # the next candidate (normally shared WASAPI) starts. The
                    # successful start is authoritative; retaining the stale
                    # error would show a failure alert beside a running stream.
                    self.status.pop("error", None)
                    for key in _STREAM_STATISTICS:
                        self.status.pop(key, None)
                    self.status["state"] = "running"
                    started = self.status.get("requested_at")
                    if started is not None:
                        self.status["startup_ms"] = round((time.monotonic() - started) * 1000)
                else:
                    self.status["fallback_count"] = self.status.get("fallback_count", 0) + 1
                    self.status["fallback_reason"] = message.get("message")
                    if message.get("cause") == "glitches":
                        self.status["glitch_fallback_count"] = self.status.get("glitch_fallback_count", 0) + 1
            elif event == "error":
                self.status.update(state="error", error=message.get("message"))

    def run_sync(self, action, *, enabled=True):
        if getattr(self.local, "token", None) is not None:
            self.check()
            return action()
        with self.condition:
            token = self._begin("starting" if enabled else "stopping", {})
            self.pending = None
        try:
            return self._execute(token, action)
        except MonitorCancelled:
            raise
        except Exception as exc:
            self.publish(token, state="error", error=str(exc))
            raise

    def _execute(self, token, action):
        with self.execution:
            self.local.token = token
            try:
                self.check()
                return action()
            finally:
                del self.local.token

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.pending is not None or self.live is not None)
                if self.pending is not None:
                    token, action = self.pending
                    self.pending = None
                else:
                    token, action = self.live
                    self.live = None
            try:
                self._execute(token, action)
            except MonitorCancelled:
                pass
            except Exception as exc:
                self.publish(token, state="error", error=str(exc))
                logger.exception("Background audio monitoring failed")
