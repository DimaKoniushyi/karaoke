import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import audio
from app.services import audio_service, monitor_worker
from app.services.monitor_control import MonitorCancelled, MonitorControl
from database import get_db
from tests.test_audio_settings import settings


@pytest.fixture
def control(monkeypatch):
    instance = MonitorControl()
    monkeypatch.setattr(audio_service, "_monitor_control", instance)
    monkeypatch.setattr(audio_service, "_monitor_process", None)
    monkeypatch.setattr(audio_service, "_monitor_reader", None)
    monkeypatch.setattr(audio_service, "_monitor_effects_disabled", False)
    monkeypatch.setattr(audio_service, "_requested_effects_disabled", False)
    yield instance
    instance.cancel()


def test_latest_request_wins_and_old_status_is_ignored(control):
    entered, release, done = threading.Event(), threading.Event(), threading.Event()
    played = []
    old_token = []

    def first():
        old_token.append(control.local.token)
        entered.set()
        assert release.wait(2)
        control.check()
        played.append("stale")

    control.submit(first)
    assert entered.wait(2)
    control.submit(lambda: played.append("intermediate"))
    control.submit(lambda: (played.append("latest"), done.set()))
    release.set()
    assert done.wait(2)
    assert played == ["latest"]
    control.event(old_token[0], {"event": "error", "message": "stale driver failure"})
    assert "error" not in control.snapshot()


@pytest.mark.parametrize("driver", ["auto", "asio"])
def test_monitor_snapshot_preserves_saved_buffer(control, monkeypatch, driver):
    original = settings(audio_driver=driver, buffer_size=128, monitoring_enabled=True)
    snapshots = []
    done = threading.Event()
    monkeypatch.setattr(audio_service, "configure_monitoring", lambda value, **_kwargs: (snapshots.append(value), done.set()))
    audio_service.request_monitoring(original)
    assert done.wait(2)
    assert snapshots[-1].buffer_size == 128
    assert original.buffer_size == 128
    done.clear()
    audio_service.request_monitoring(original)
    assert done.wait(2)
    assert snapshots[-1].buffer_size == 128


def test_monitor_statistics_reset_on_restart(control):
    control.event(None, {"event": "level", "callback_frames": 480, "callback_count": 12, "glitch_count": 2})
    assert control.snapshot()["callback_frames"] == 480
    assert control.snapshot()["glitch_count"] == 2
    control.event(None, {"event": "started", "input_latency_ms": 5, "output_latency_ms": 7})
    assert "glitch_count" not in control.snapshot()
    assert control.snapshot()["input_latency_ms"] == 5


def test_native_engine_frame_counters_reach_the_snapshot(control):
    # native_wasapi.py's pump() reports captured_frames/rendered_frames on
    # every "level" event, but the allowlist copying per-block telemetry
    # into status used to omit them -- the native engine's own frame counts
    # could never reach the API/frontend even though they were computed and
    # emitted every ~100ms.
    control.event(None, {"event": "level", "captured_frames": 48_000, "rendered_frames": 47_999})
    assert control.snapshot()["captured_frames"] == 48_000
    assert control.snapshot()["rendered_frames"] == 47_999


def test_legacy_http_auto_buffer_cannot_override_saved_buffer(monkeypatch):
    current = settings(monitoring_enabled=True, noise_suppression=0.35, octave=0)
    start = Mock(return_value=current)
    monkeypatch.setattr(audio_service, "set_monitoring_enabled", start)
    app = FastAPI()
    app.include_router(audio.router)
    db = Mock()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        response = client.post("/audio/direct-monitor/start?auto_buffer=true")
    assert response.status_code == 202
    start.assert_called_once_with(db, True, disabled_effects=False, background=True)


def test_hardware_suspend_releases_monitor_without_changing_saved_preference(monkeypatch):
    current = settings(monitoring_enabled=True, noise_suppression=0.35, octave=0)
    db = Mock()
    stop = Mock()
    resume = Mock()
    monkeypatch.setattr(audio_service, "get_settings", lambda _db: current)
    monkeypatch.setattr(audio_service, "suspend_monitoring", stop)
    monkeypatch.setattr(audio_service, "resume_monitoring", resume)
    app = FastAPI()
    app.include_router(audio.router)
    app.dependency_overrides[get_db] = lambda: db

    with TestClient(app) as client:
        suspended = client.post("/audio/direct-monitor/suspend")
        restored = client.post("/audio/direct-monitor/resume")

    assert suspended.status_code == 200
    assert restored.status_code == 202
    assert suspended.json()["monitoring_enabled"] is True
    assert restored.json()["monitoring_enabled"] is True
    stop.assert_called_once_with()
    resume.assert_called_once_with(current)


def test_monitor_requests_stay_dormant_while_window_hardware_is_suspended(control, monkeypatch):
    monkeypatch.setattr(audio_service, "_hardware_suspended", True)
    configure = Mock()
    monkeypatch.setattr(audio_service, "configure_monitoring", configure)

    audio_service.request_monitoring(settings(monitoring_enabled=True))
    with control.execution:
        pass

    configure.assert_not_called()
    assert control.snapshot()["state"] == "idle"


def test_stop_during_device_enumeration_never_launches_worker(control, monkeypatch):
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def query():
        entered.set()
        assert release.wait(2)
        return []

    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service.sd, "query_devices", query)
    launch = Mock()
    monkeypatch.setattr(audio_service, "_start_monitor_worker", launch)
    audio_service.request_monitoring(settings(monitoring_enabled=True))
    assert entered.wait(2)
    audio_service.stop_monitoring()
    release.set()
    # A barrier in the same execution lane proves the cancelled request has ended.
    control.update_live(finished.set)
    # update_live on a cancelled token intentionally does nothing; execution lock
    # waits for the in-flight enumeration instead of relying on a sleep.
    with control.execution:
        pass
    launch.assert_not_called()
    assert control.snapshot()["state"] == "idle"


def test_http_accepts_start_and_settings_while_hardware_is_blocked(control, monkeypatch):
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    current = settings(monitoring_enabled=False, noise_suppression=0.35, octave=0)
    db = Mock()
    db.get.return_value = current
    monkeypatch.setattr(audio_service, "commit_refresh", lambda _db, item: item)

    def configure(_settings, **_kwargs):
        assert isinstance(_settings, SimpleNamespace)
        entered.set()
        assert release.wait(3)
        completed.set()
        control.check()

    monkeypatch.setattr(audio_service, "_configure_monitoring", configure)
    app = FastAPI()
    app.include_router(audio.router)
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        try:
            start = time.perf_counter()
            response = client.post("/audio/direct-monitor/start")
            elapsed = time.perf_counter() - start
            assert response.status_code == 202
            assert elapsed < 0.2
            assert entered.wait(2)
            assert response.json()["monitoring_enabled"] is True
            assert client.get("/audio/direct-monitor/status").json()["state"] == "starting"
            query = Mock(side_effect=AssertionError("No device query in settings HTTP request"))
            monkeypatch.setattr(audio_service.sd, "query_devices", query)
            start = time.perf_counter()
            response = client.post("/audio/settings", json={"input_device_id": 2})
            assert response.status_code == 200
            assert time.perf_counter() - start < 0.2
            assert client.post("/audio/direct-monitor/stop").status_code == 200
            print(f"\nAccepted monitor start with blocked hardware: {elapsed * 1000:.1f} ms")
        finally:
            control.cancel()
            release.set()
            assert completed.wait(2)


def test_one_enumeration_and_no_parent_format_probes(control, monkeypatch):
    devices = [
        {"name": "USB mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000},
        {"name": "USB speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
    ]
    query = Mock(return_value=devices)
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service.sd, "query_devices", query)
    monkeypatch.setattr(audio_service.sd, "query_hostapis", lambda _: {"name": "Windows WASAPI"})
    monkeypatch.setattr(audio_service.sd, "default", SimpleNamespace(device=(0, 1)))
    monkeypatch.setattr(audio_service.sd, "check_input_settings", Mock(side_effect=AssertionError("No parent probe")))
    launch = Mock()
    monkeypatch.setattr(audio_service, "_start_monitor_worker", launch)
    audio_service.configure_monitoring(settings(monitoring_enabled=True, reverb=.3, echo=.4, delay=.2))
    query.assert_called_once_with()
    options = launch.call_args.args[0]
    assert options["wasapi_mode"] == "shared"
    assert (options["reverb"], options["echo"], options["delay"]) == (.3, .4, .2)


def test_mme_buffer_is_floored_to_avoid_glitching_below_a_safe_minimum(control, monkeypatch):
    devices = [
        {"name": "MME mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 44100},
        {"name": "MME speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 44100},
    ]
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service.sd, "query_devices", Mock(return_value=devices))
    monkeypatch.setattr(audio_service.sd, "query_hostapis", lambda _: {"name": "MME"})
    monkeypatch.setattr(audio_service.sd, "default", SimpleNamespace(device=(0, 1)))
    launch = Mock()
    monkeypatch.setattr(audio_service, "_start_monitor_worker", launch)

    audio_service.configure_monitoring(settings(monitoring_enabled=True, buffer_size=16, audio_driver="mme"))

    options = launch.call_args.args[0]
    assert options["wasapi_mode"] == "plain"
    assert options["blocksize"] == audio_service._PLAIN_HOST_MIN_BLOCKSIZE

    # A buffer already at or above the floor passes through unchanged.
    launch.reset_mock()
    audio_service.configure_monitoring(settings(monitoring_enabled=True, buffer_size=2048, audio_driver="mme"))
    assert launch.call_args.args[0]["blocksize"] == 2048


def test_wasapi_buffer_is_never_floored_even_when_small(control, monkeypatch):
    devices = [
        {"name": "USB mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000},
        {"name": "USB speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
    ]
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service.sd, "query_devices", Mock(return_value=devices))
    monkeypatch.setattr(audio_service.sd, "query_hostapis", lambda _: {"name": "Windows WASAPI"})
    monkeypatch.setattr(audio_service.sd, "default", SimpleNamespace(device=(0, 1)))
    launch = Mock()
    monkeypatch.setattr(audio_service, "_start_monitor_worker", launch)

    audio_service.configure_monitoring(settings(monitoring_enabled=True, buffer_size=16))

    assert launch.call_args.args[0]["blocksize"] == 16


def test_live_changes_during_start_are_coalesced_without_restart(control, monkeypatch):
    entered, release, updated = threading.Event(), threading.Event(), threading.Event()
    def start():
        entered.set()
        assert release.wait(2)
        control.publish(state="running")
    control.submit(start)
    assert entered.wait(2)
    current = settings(monitoring_enabled=True, noise_suppression=.35, octave=0)
    monkeypatch.setattr(audio_service, "_get_or_create_settings", lambda _: current)
    monkeypatch.setattr(audio_service, "commit_refresh", lambda _db, item: item)
    calls = []
    monkeypatch.setattr(audio_service, "_send_live_update", lambda payload: (calls.append(payload), updated.set()))
    audio_service.update_settings(Mock(), {"echo": .2}, background=True)
    audio_service.update_settings(Mock(), {"reverb": .6}, background=True)
    release.set()
    assert updated.wait(2)
    assert len(calls) == 1
    assert calls[0]["echo"] == .2 and calls[0]["reverb"] == .6
    assert control.snapshot()["state"] == "running"


def test_status_distinguishes_driver_rejection_and_repeated_glitches(control):
    control.run_sync(lambda: None)
    token = control.token
    control.event(token, {"event": "fallback", "cause": "device-open", "message": "exclusive unavailable"})
    assert control.snapshot()["glitch_fallback_count"] == 0
    for _ in range(2):
        control.event(token, {"event": "fallback", "cause": "glitches", "message": "underflows"})
    control.event(token, {"event": "started", "mode": "shared", "blocksize": 256,
                          "sample_rate": 48000, "input_latency_ms": 5, "output_latency_ms": 8})
    result = control.snapshot()
    assert result["fallback_count"] == 3 and result["glitch_fallback_count"] == 2
    assert result["state"] == "running" and result["output_latency_ms"] == 8
    assert "startup_ms" in result
    control.cancel()
    control.event(token, {"event": "started"})
    assert control.snapshot()["state"] == "idle"


def test_cancelled_launch_does_not_spawn_a_process(control, monkeypatch, tmp_path):
    launch = Mock()
    monkeypatch.setattr(audio_service.subprocess, "Popen", launch)
    token = threading.Event()
    token.set()
    control.local.token = token
    try:
        with pytest.raises(MonitorCancelled):
            audio_service._launch_monitor_process(["unused"], cwd=tmp_path)
    finally:
        del control.local.token
    launch.assert_not_called()


def test_wasapi_always_opens_shared_with_correct_labels(monkeypatch):
    monkeypatch.setattr(monitor_worker.sd, "WasapiSettings", lambda **kwargs: kwargs)
    base = {"sample_rate": 48000, "output_channels": 2, "input_device_id": 0,
            "output_device_id": 1, "blocksize": 128, "wasapi_mode": "shared"}
    shared = monitor_worker._stream_candidate(base)
    assert shared["extra_settings"] == ({"exclusive": False, "auto_convert": True}, {"exclusive": False, "auto_convert": True})
    for mode in ("exclusive", "input-exclusive"):
        with pytest.raises(ValueError, match="Unsupported WASAPI mode"):
            monitor_worker._stream_candidate({**base, "wasapi_mode": mode})
    details = monitor_worker._stream_diagnostics(SimpleNamespace(latency=(.004, .006)), shared, base, "shared")
    assert details["input_latency_ms"] == 4 and details["output_latency_ms"] == 6


def test_fallback_never_decreases_explicit_buffer():
    base = {"sample_rate": 44100, "output_channels": 2, "input_device_id": 0,
            "output_device_id": 1, "blocksize": 512}
    candidate = monitor_worker._stream_candidate(base)
    assert candidate["blocksize"] == 0 or candidate["blocksize"] >= 512


def test_format_fallbacks_keep_native_rate_first():
    base = {"sample_rate": 44100, "sample_rates": [44100, 48000], "output_channels": 2,
            "input_device_id": 0, "output_device_id": 1, "blocksize": 128}
    candidate = monitor_worker._stream_candidate(base)
    assert candidate["samplerate"] == 44100


def test_asio_settings_do_not_enumerate_drivers_in_http_thread(control, monkeypatch):
    current = settings(monitoring_enabled=False)
    monkeypatch.setattr(audio_service, "_get_or_create_settings", lambda _: current)
    monkeypatch.setattr(audio_service, "commit_refresh", lambda _db, item: item)
    enumerate_drivers = Mock(side_effect=AssertionError("Slow hardware enumeration in HTTP"))
    monkeypatch.setattr(audio_service, "list_asio_drivers", enumerate_drivers)
    saved = audio_service.update_settings(Mock(), {"audio_driver": "asio", "asio_driver_name": "Studio ASIO"}, background=True)
    assert saved.asio_driver_name == "Studio ASIO"
    enumerate_drivers.assert_not_called()


def test_async_hardware_error_is_reported_without_losing_controller(control):
    entered, release, barrier = threading.Event(), threading.Event(), threading.Event()
    def failing_start():
        entered.set()
        assert release.wait(2)
        raise RuntimeError("USB endpoint unavailable")
    control.submit(failing_start)
    assert entered.wait(2)
    control.update_live(barrier.set)
    release.set()
    assert barrier.wait(2)
    assert control.snapshot()["state"] == "error"
    assert control.snapshot()["error"] == "USB endpoint unavailable"
    recovered = threading.Event()
    control.submit(lambda: (control.publish(state="running"), recovered.set()))
    assert recovered.wait(2)
    assert control.snapshot()["state"] == "running"


def test_existing_asio_protocol_is_normalized_without_native_changes(control):
    control.run_sync(lambda: None)
    control.event(control.token, {"event": "started", "driver": "Studio ASIO",
                                "sample_rate": 48000, "buffer_size": 128,
                                "input_latency": 240, "output_latency": 480})
    status = control.snapshot()
    assert status["mode"] == "ASIO" and status["blocksize"] == 128
    assert status["input_latency_ms"] == 5 and status["output_latency_ms"] == 10


def test_asio_latency_uses_reported_samples_not_requested_buffer(control):
    control.event(control.token, {"event": "started", "driver": "Studio ASIO",
                                 "sample_rate": 44100, "buffer_size": 64,
                                 "input_latency": 130, "output_latency": 154})
    status = control.snapshot()
    assert status["input_latency_ms"] == pytest.approx(130000 / 44100)
    assert status["output_latency_ms"] == pytest.approx(154000 / 44100)
    assert status["latency_source"] == "asio-driver-report"


@pytest.mark.parametrize("rate,samples", [(0, 128), (44100, -1), (44100, None), (float("nan"), 128)])
def test_invalid_asio_latency_is_not_reported_as_zero(control, rate, samples):
    control.event(control.token, {"event": "started", "sample_rate": rate, "buffer_size": 128,
                                 "input_latency": samples, "output_latency": samples})
    status = control.snapshot()
    assert "input_latency_ms" not in status
    assert "output_latency_ms" not in status


def test_asio_buffer_estimate_is_preserved_when_driver_rejects_latency_query(control):
    control.event(control.token, {"event": "started", "driver": "Realtek ASIO",
                                 "sample_rate": 48000, "buffer_size": 64,
                                 "input_latency": 64, "output_latency": 64,
                                 "latency_source": "asio-buffer-estimate"})
    status = control.snapshot()
    assert status["latency_source"] == "asio-buffer-estimate"
    assert status["input_latency_ms"] == pytest.approx(64 * 1000 / 48000)
    assert status["output_latency_ms"] == pytest.approx(64 * 1000 / 48000)


def test_unusable_asio_driver_falls_back_to_shared_monitor(control, monkeypatch):
    from app.services import recording_service

    current = settings(
        audio_driver="asio", asio_driver_name="Realtek ASIO", monitoring_enabled=True
    )
    shared = Mock()
    monkeypatch.setattr(recording_service, "apply_monitor_settings", lambda *_: False)
    monkeypatch.setattr(
        audio_service,
        "_start_asio_monitor",
        Mock(side_effect=RuntimeError("Could not start ASIO full-duplex stream")),
    )
    monkeypatch.setattr(audio_service, "_start_shared_monitor", shared, raising=False)

    audio_service.configure_monitoring(current)

    shared.assert_called_once_with(current, driver="auto", relay_needed=False)
    status = control.snapshot()
    assert status["fallback_count"] == 1
    assert status["requested_driver"] == "Realtek ASIO"
    assert status["failed_driver"] == "Realtek ASIO"
    assert "full-duplex" in status["fallback_reason"]


def test_split_queue_statistics_are_separate_and_reset_after_fallback(control):
    control.run_sync(lambda: None)
    control.event(control.token, {"event": "started", "engine": "wasapi-split", "input_latency_ms": 5.8, "output_latency_ms": 5.8})
    control.event(control.token, {"event": "level", "queue_ms": 2.9, "queue_capacity_ms": 11.6, "queue_underruns": 6})
    status = control.snapshot()
    assert status["engine"] == "wasapi-split"
    assert status["input_latency_ms"] + status["output_latency_ms"] == 11.6
    assert status["queue_ms"] == 2.9
    control.event(control.token, {"event": "started", "engine": "duplex"})
    assert "queue_ms" not in control.snapshot()
    assert "queue_underruns" not in control.snapshot()


def test_native_stage_timings_are_exposed_and_cleared_on_restart(control):
    from app.services.native_wasapi import TIMING_FIELDS
    values = {name: index / 10 for index, name in enumerate(TIMING_FIELDS)}
    control.event(control.token, {"event": "started", "engine": "wasapi-native-shared"})
    control.event(control.token, {"event": "level", **values})
    status = control.snapshot()
    assert all(status[name] == value for name, value in values.items())
    control.event(control.token, {"event": "started", "engine": "wasapi-native-shared"})
    assert not any(name in control.snapshot() for name in values)


def test_native_latency_breakdown_is_logged_once_per_connection(control, caplog):
    control.event(control.token, {
        "event": "started", "engine": "wasapi-native-shared",
        "input_period_frames": 480, "output_period_frames": 144,
        "sample_rate": 48000, "output_sample_rate": 48000,
    })
    level = {
        "event": "level", "stream_latency_ms": 68.879,
        "capture_delivery_ms": 43.2, "program_residence_ms": 0.4,
        "queue_residence_ms": 0.1, "output_clock_lead_ms": 25.1,
        "render_padding_ms": 3.0, "dsp_compute_ms": 0.2,
    }
    with caplog.at_level("INFO", logger="app.services.monitor_control"):
        control.event(control.token, level)
        control.event(control.token, level)
    records = [record.message for record in caplog.records if "WASAPI latency breakdown" in record.message]
    assert len(records) == 1
    assert "stream_ms=68.879" in records[0]
    assert "capture_ms=43.2" in records[0]
    assert "input_period=480@48000" in records[0]
    assert "output_period=144@48000" in records[0]


def test_start_shared_monitor_opens_a_relay_and_passes_its_port_to_the_worker(control, monkeypatch):
    monkeypatch.setattr(audio_service, "_monitor_relay", None)
    devices = [
        {"name": "USB mic", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0, "default_samplerate": 48000},
        {"name": "USB speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2, "default_samplerate": 48000},
    ]
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service.sd, "query_devices", Mock(return_value=devices))
    monkeypatch.setattr(audio_service.sd, "query_hostapis", lambda _: {"name": "Windows WASAPI"})
    monkeypatch.setattr(audio_service.sd, "default", SimpleNamespace(device=(0, 1)))
    launch = Mock()
    monkeypatch.setattr(audio_service, "_start_monitor_worker", launch)

    # A relay is only ever opened for a caller that actually asks for one
    # (room mode) -- plain monitoring must not pay for it, see relay_needed.
    audio_service.configure_monitoring(settings(monitoring_enabled=True), relay_needed=True)

    options = launch.call_args.args[0]
    relay = audio_service._monitor_relay
    assert relay is not None
    assert options["audio_relay_port"] == relay.port
    relay.close()


def test_start_shared_monitor_replaces_a_stale_relay_that_stop_did_not_clear(control, monkeypatch):
    from app.services.audio_relay import AudioRelayServer

    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    stale = AudioRelayServer()
    monkeypatch.setattr(audio_service, "_monitor_relay", stale)
    try:
        opened = audio_service._open_monitor_relay()
        assert opened is not stale
        assert audio_service._monitor_relay is opened
    finally:
        opened.close()


def test_stop_monitoring_closes_and_clears_the_relay(control, monkeypatch):
    from app.services.audio_relay import AudioRelayServer

    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    relay = AudioRelayServer()
    monkeypatch.setattr(audio_service, "_monitor_relay", relay)
    audio_service.stop_monitoring()
    assert audio_service._monitor_relay is None
    # A closed AudioRelayServer's accept loop has exited; opening a new
    # connection to it must fail rather than hang.
    import socket
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", relay.port), timeout=1.0)


def test_subscribe_monitor_relay_reflects_whether_a_relay_is_currently_running(control, monkeypatch):
    from app.services.audio_relay import AudioRelayServer

    monkeypatch.setattr(audio_service, "_monitor_relay", None)
    assert audio_service.subscribe_monitor_relay() is None

    relay = AudioRelayServer()
    monkeypatch.setattr(audio_service, "_monitor_relay", relay)
    try:
        result = audio_service.subscribe_monitor_relay()
        assert result is not None
        subscribed_relay, subscriber_queue = result
        assert subscribed_relay is relay
        assert subscriber_queue.empty()
    finally:
        relay.close()
