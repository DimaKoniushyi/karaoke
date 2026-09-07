import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import audio_service
from tests._shared import patch_attrs, patch_many, raises


class OutputLines(list):
    def __init__(self, *lines):
        super().__init__(lines)
        self.closed = False

    def close(self):
        self.closed = True


def process(*lines, poll=None): return SimpleNamespace(stdout=OutputLines(*lines), stdin=Mock(), poll=Mock(return_value=poll), terminate=Mock(), kill=Mock(), wait=Mock())


def test_stop_monitoring_handles_idle_finished_and_running_processes(monkeypatch):
    patch_attrs(monkeypatch, audio_service, _monitor_process=None, _monitor_reader=None)
    audio_service.stop_monitoring()

    finished, finished_reader = process(poll=0), Mock()
    patch_attrs(monkeypatch, audio_service, _monitor_process=finished, _monitor_reader=finished_reader)
    audio_service.stop_monitoring()
    finished.terminate.assert_not_called()
    # A worker that already exited on its own (driver crash, unplugged
    # device) still needs its pipes closed and reader joined -- an earlier
    # bug returned right after the poll() check and skipped this entirely.
    assert finished.stdout.closed
    finished_reader.join.assert_called_once_with(timeout=1.0)

    running, reader = process(poll=None), Mock()
    patch_attrs(monkeypatch, audio_service, _monitor_process=running, _monitor_reader=reader)
    audio_service.stop_monitoring()
    running.terminate.assert_called_once_with()
    running.wait.assert_called_once_with(timeout=1.5)
    assert running.stdout.closed
    reader.join.assert_called_once_with(timeout=1.0)


def test_stop_monitoring_kills_timeout_and_tolerates_os_error(monkeypatch):
    timed_out = process(poll=None)
    timed_out.wait.side_effect = [subprocess.TimeoutExpired("worker", 1.5), None]
    patch_attrs(monkeypatch, audio_service, _monitor_process=timed_out, _monitor_reader=None)
    audio_service.stop_monitoring()
    timed_out.kill.assert_called_once_with()
    assert timed_out.wait.call_count == 2

    failed = process(poll=None)
    failed.terminate.side_effect = OSError("access denied")
    monkeypatch.setattr(audio_service, "_monitor_process", failed)
    audio_service.stop_monitoring()
    assert failed.stdout.closed


def test_stop_monitoring_joins_reader_before_closing_stdout(monkeypatch):
    # The reader thread iterates `for line in process.stdout`; closing that
    # file out from under a REAL (non-mock) reader thread while it's still
    # iterating can raise inside that thread. Assert the actual call order,
    # not just that both eventually happen.
    running = process(poll=None)
    call_order = []
    running.stdout.close = Mock(side_effect=lambda: call_order.append("close"))
    reader = SimpleNamespace(join=Mock(side_effect=lambda **_kw: call_order.append("join")))
    patch_attrs(monkeypatch, audio_service, _monitor_process=running, _monitor_reader=reader)
    audio_service.stop_monitoring()
    assert call_order == ["join", "close"]


def test_frozen_backend_spawns_its_internal_audio_monitor_mode(monkeypatch, tmp_path):
    backend = tmp_path / "KaraokeBackend.exe"
    backend.write_bytes(b"backend")
    patch_many(monkeypatch, (audio_service.config, "IS_FROZEN", True), (audio_service.sys, "executable", str(backend)))
    launch = Mock()
    monkeypatch.setattr(audio_service, "_launch_monitor_process", launch)
    audio_service._start_monitor_worker({"gain": 1})
    command = launch.call_args.args[0]
    assert command[:3] == [str(backend), "--audio-monitor", "--config"]
    assert json.loads(command[-1]) == {"gain": 1}


def test_monitor_process_consumes_started_levels_and_invalid_output(monkeypatch, tmp_path):
    worker = process(
        "not-json\n",
        '[]\n',
        'null\n',
        '{"event":"stage","stage":"open shared endpoints"}\n',
        '{"event":"started"}\n',
        '{"event":"level","rms_db":-8,"clipping":true,"extra":1}\n',
        poll=None,
    )
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    patch_attrs(monkeypatch, audio_service, _monitor_signal={'rms_db': -120.0, 'clipping': False, 'silent': True})
    consumed, release = threading.Event(), threading.Event()
    class LiveOutput(OutputLines):
        def __iter__(self):
            yield from super().__iter__()
            consumed.set()
            release.wait(2)
    worker.stdout = LiveOutput(*worker.stdout)

    audio_service._launch_monitor_process(["worker"], cwd=tmp_path)
    assert consumed.wait(2)

    creationflags = audio_service.subprocess.Popen.call_args.kwargs["creationflags"]
    # Plain process priority -- only the realtime audio thread itself should
    # ever be boosted (MMCSS "Pro Audio" in monitor.cpp, or PortAudio's own
    # internal handling for the other engines), never the whole process.
    assert creationflags & getattr(subprocess, "HIGH_PRIORITY_CLASS", 0) == 0
    assert creationflags & getattr(subprocess, "CREATE_NO_WINDOW", 0) == getattr(
        subprocess, "CREATE_NO_WINDOW", 0
    )
    # NORMAL_PRIORITY_CLASS must be explicit, not merely absent: with no
    # priority flag at all Windows has the child inherit the calling
    # process's CURRENT priority class, so a monitor launched while
    # pipeline_service's AI-job BELOW_NORMAL_PRIORITY_CLASS hadn't been
    # restored yet would otherwise start -- and silently stay -- at
    # BELOW_NORMAL for its whole session.
    assert creationflags & getattr(subprocess, "NORMAL_PRIORITY_CLASS", 0) == getattr(
        subprocess, "NORMAL_PRIORITY_CLASS", 0
    )

    assert audio_service._monitor_signal == {
        "rms_db": -8,
        "clipping": True,
        "silent": True,
    }
    release.set()
    audio_service._monitor_reader.join(timeout=2)
    assert audio_service._monitor_signal["silent"] is True


def test_asio_driver_reset_restarts_the_monitor_instead_of_reporting_an_error(monkeypatch, tmp_path):
    worker = process(
        '{"event":"started"}\n',
        '{"event":"reset_requested"}\n',
        '{"event":"stopped","reset_requested":true}\n',
        poll=0,
    )
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    on_driver_reset = Mock()

    audio_service._launch_monitor_process(["worker"], cwd=tmp_path, on_driver_reset=on_driver_reset)
    audio_service._monitor_reader.join(timeout=2)

    on_driver_reset.assert_called_once_with()
    assert audio_service.monitoring_status().get("state") != "error"


def test_asio_normal_stop_does_not_trigger_a_restart(monkeypatch, tmp_path):
    worker = process(
        '{"event":"started"}\n',
        '{"event":"stopped","reset_requested":false}\n',
        poll=0,
    )
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    on_driver_reset = Mock()

    audio_service._launch_monitor_process(["worker"], cwd=tmp_path, on_driver_reset=on_driver_reset)
    audio_service._monitor_reader.join(timeout=2)

    on_driver_reset.assert_not_called()
    assert audio_service.monitoring_status().get("state") == "error"


def test_asio_reset_does_not_restart_a_process_already_superseded(monkeypatch, tmp_path):
    release = threading.Event()

    class StalledOutput(OutputLines):
        def __iter__(self):
            yield '{"event":"started"}\n'
            release.wait(2)
            yield '{"event":"stopped","reset_requested":true}\n'

    worker = process(poll=0)
    worker.stdout = StalledOutput()
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    on_driver_reset = Mock()

    audio_service._launch_monitor_process(["worker"], cwd=tmp_path, on_driver_reset=on_driver_reset)
    # A newer monitor already replaced this one (e.g. the user changed
    # settings right as the driver reset) -- the stale process's own reset
    # must not resurrect it.
    monkeypatch.setattr(audio_service, "_monitor_process", Mock())
    release.set()
    audio_service._monitor_reader.join(timeout=2)

    on_driver_reset.assert_not_called()


def test_started_event_calls_on_buffer_negotiated_with_the_reported_size(monkeypatch, tmp_path):
    worker = process('{"event":"started","buffer_size":256}\n', poll=0)
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    on_buffer_negotiated = Mock()

    audio_service._launch_monitor_process(
        ["worker"], cwd=tmp_path, on_buffer_negotiated=on_buffer_negotiated
    )
    audio_service._monitor_reader.join(timeout=2)

    on_buffer_negotiated.assert_called_once_with(256)


def test_started_event_without_a_buffer_callback_does_not_raise(monkeypatch, tmp_path):
    worker = process('{"event":"started","buffer_size":256}\n', poll=0)
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))

    audio_service._launch_monitor_process(["worker"], cwd=tmp_path)
    audio_service._monitor_reader.join(timeout=2)


def test_started_event_with_a_missing_buffer_size_does_not_call_the_callback(monkeypatch, tmp_path):
    worker = process('{"event":"started"}\n', poll=0)
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    on_buffer_negotiated = Mock()

    audio_service._launch_monitor_process(
        ["worker"], cwd=tmp_path, on_buffer_negotiated=on_buffer_negotiated
    )
    audio_service._monitor_reader.join(timeout=2)

    on_buffer_negotiated.assert_not_called()


def test_started_log_prefers_the_negotiated_wasapi_period_over_the_raw_request(monkeypatch, tmp_path, caplog):
    # blocksize on the native WASAPI engine is the raw requested value
    # echoed back (Info.blocksize in monitor.cpp); input_period_frames is
    # what GetSharedModeEnginePeriod actually negotiated with the device --
    # the log must report the latter, or a user changing the buffer setting
    # sees the same "buffer_size" logged even when it made no difference.
    worker = process(
        '{"event":"started","engine":"wasapi-native-shared","blocksize":256,'
        '"input_period_frames":441,"output_period_frames":441,"sample_rate":44100}\n',
        poll=0,
    )
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))

    with caplog.at_level("INFO", logger="app.services.audio_service"):
        audio_service._launch_monitor_process(["worker"], cwd=tmp_path)
        audio_service._monitor_reader.join(timeout=2)

    [record] = [r for r in caplog.records if "Audio monitor started" in r.message]
    assert "buffer_size=441" in record.message
    assert "driver=wasapi-native-shared" in record.message


def test_started_log_falls_back_to_blocksize_when_no_period_was_reported(monkeypatch, tmp_path, caplog):
    worker = process('{"event":"started","engine":"wasapi-split","blocksize":128}\n', poll=0)
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))

    with caplog.at_level("INFO", logger="app.services.audio_service"):
        audio_service._launch_monitor_process(["worker"], cwd=tmp_path)
        audio_service._monitor_reader.join(timeout=2)

    [record] = [r for r in caplog.records if "Audio monitor started" in r.message]
    assert "buffer_size=128" in record.message


def test_monitor_process_reports_worker_error_and_early_exit(monkeypatch, tmp_path):
    stop = Mock()
    monkeypatch.setattr(audio_service, "_stop_monitoring_process", stop)
    erroring = process('{"event":"error","message":"device busy"}\n', poll=1)
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=erroring))
    raises(RuntimeError, lambda: audio_service._launch_monitor_process(['worker'], cwd=tmp_path), match='device busy')
    stop.assert_called_once_with(expected_process=erroring)

    stop.reset_mock()
    exited = process(poll=1)
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=exited))
    raises(RuntimeError, lambda: audio_service._launch_monitor_process(['worker'], cwd=tmp_path), match='terminated during startup')
    stop.assert_called_once_with(expected_process=exited)


def test_monitor_process_times_out_when_reader_never_signals(monkeypatch, tmp_path):
    worker = process(poll=None)
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    event = Mock()
    event.wait.return_value = False
    monkeypatch.setattr(audio_service.threading, "Event", Mock(return_value=event))
    reader = Mock()
    monkeypatch.setattr(audio_service.threading, "Thread", Mock(return_value=reader))
    stop = Mock()
    monkeypatch.setattr(audio_service, "_stop_monitoring_process", stop)

    raises(RuntimeError, lambda: audio_service._launch_monitor_process(['worker'], cwd=tmp_path), match='Timed out')

    reader.start.assert_called_once_with()
    stop.assert_called_once_with(expected_process=worker)


def test_stop_monitoring_does_not_join_current_reader(monkeypatch):
    running, current = process(poll=None), threading.current_thread()
    patch_attrs(monkeypatch, audio_service, _monitor_process=running, _monitor_reader=current)
    audio_service.stop_monitoring()


def test_timeout_identifies_last_startup_stage(monkeypatch, tmp_path):
    released = threading.Event()
    class StalledOutput(OutputLines):
        def __iter__(self):
            yield '{"event":"stage","stage":"initialize microphone DSP"}\n'
            released.wait(2)
    worker = process()
    worker.stdout = StalledOutput()
    monkeypatch.setattr(audio_service.subprocess, "Popen", Mock(return_value=worker))
    monkeypatch.setattr(audio_service, "_MONITOR_START_TIMEOUT_SECONDS", .1)
    monkeypatch.setattr(audio_service, "_stop_monitoring_process", Mock())
    try:
        raises(RuntimeError, lambda: audio_service._launch_monitor_process(["worker"], cwd=tmp_path),
               match="stage=initialize microphone DSP")
    finally:
        released.set()
        audio_service._monitor_reader.join(2)


def test_built_worker_reaches_native_device_validation_without_ai():
    from pathlib import Path

    import pytest
    repository = Path(__file__).resolve().parents[2]
    executable = (repository /
                  "generated/build/backend/dist/KaraokeBackend/KaraokeBackend.exe")
    if (
        not executable.is_file()
        or not executable.with_name("KaraokeWasapi.dll").is_file()
        or executable.stat().st_mtime < (repository / "backend/run.py").stat().st_mtime
    ):
        pytest.skip("Packaged monitor smoke artifact not built")
    # Deliberately nonexistent endpoints: exercise packaged imports + native
    # startup without recording audio or playing anything on the user's device.
    config = {"sample_rate": 48000, "output_channels": 2, "input_device_id": 0,
              "output_device_id": 0, "blocksize": 64, "gain": 0, "wasapi_mode": "shared",
              "native_shared": True, "input_device_name": "__missing_test_microphone_76c22__",
              "output_device_name": "__missing_test_speakers_76c22__"}
    result = subprocess.run([str(executable), "--audio-monitor", "--config", json.dumps(config)],
                            capture_output=True, text=True, encoding="utf-8", timeout=12)
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert result.returncode == 1
    assert any(event.get("stage") == "load native WASAPI and open shared endpoints" for event in events)
    assert events[-1]["event"] == "error"
    assert events[-1]["message"] == "Selected audio endpoint is unavailable; no default-device substitution"
