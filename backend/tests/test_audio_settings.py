import builtins
import importlib
from pathlib import Path
from unittest.mock import Mock

import numpy as np
import pytest
from sqlalchemy.exc import IntegrityError

import models
from app.services import audio_service
from tests._shared import patch_attrs, patch_many, raises


@pytest.fixture(autouse=True)
def reset_process_wide_audio_state(monkeypatch):
    """These globals intentionally persist in production, but not between tests."""
    monkeypatch.setattr(audio_service, "_monitor_relay_needed", False)
    monkeypatch.setattr(
        audio_service,
        "_signal_probe_cache",
        {"at": 0.0, "device": object(), "gain": None, "result": None},
    )
    audio_service._monitor_control.publish(state="idle")


def settings(**changes):
    values = {
        "id": 1,
        "input_device_id": None,
        "input_device_name": None,
        "output_device_id": None,
        "volume": 1.0,
        "sensitivity": 0.5,
        "latency_ms": 50,
        "audio_driver": "auto",
        "asio_driver_name": None,
        "buffer_size": 64,
        "monitoring_enabled": False,
        "reverb": 0.0,
        "echo": 0.0,
        "delay": 0.0,
    }
    values.update(changes)
    return models.AudioSettings(**values)


def test_settings_are_loaded_or_created(monkeypatch):
    database, existing = Mock(), settings()
    database.get.return_value = existing
    assert audio_service.get_settings(database) is existing

    database.get.return_value = None
    commit = Mock(side_effect=lambda _db, item: item)
    monkeypatch.setattr(audio_service, "commit_refresh", commit)
    created = audio_service.get_settings(database)
    assert isinstance(created, models.AudioSettings) and created.id == 1
    database.add.assert_called_once_with(created)


def test_settings_creation_recovers_from_concurrent_singleton_insert(monkeypatch):
    database, winner = Mock(), settings()
    database.get.side_effect = [None, winner]
    monkeypatch.setattr(
        audio_service,
        "commit_refresh",
        Mock(side_effect=IntegrityError("insert", {}, Exception("duplicate"))),
    )

    assert audio_service.get_settings(database) is winner
    database.add.assert_called_once()
    assert database.get.call_count == 2


def test_input_device_name_is_bounded_and_backend_aware(monkeypatch):
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", False)
    assert audio_service._input_device_name(0) is None
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    patch_attrs(monkeypatch, audio_service.sd, query_devices=Mock(return_value=[{'name': 'Mic'}, {'name': ''}]))
    assert (audio_service._input_device_name(0) == 'Mic') and (audio_service._input_device_name(1) is None) and (audio_service._input_device_name(3) is None) and (audio_service._input_device_name(None) is None)


def test_normalized_settings_patch_handles_defaults_devices_and_asio(monkeypatch):
    current = settings(input_device_id=1, input_device_name="Old", output_device_id=2)
    patch_attrs(monkeypatch, audio_service, _AUDIO_BACKEND_AVAILABLE=True)
    patch_attrs(
        monkeypatch, audio_service.sd,
        query_devices=Mock(return_value=[{}, {}, {}, {'name': 'New'}, {}, {'name': 'New Speakers'}]),
    )
    updates, changed = audio_service._normalized_settings_patch(
        current,
        {
            "input_device_id": None,
            "output_device_id": None,
            "volume": None,
            "sensitivity": 0.5,
        },
    )
    assert (updates, changed) == (
        {
            'input_device_id': None, 'input_device_name': None,
            'output_device_id': None, 'output_device_name': None,
        },
        {'input_device_id', 'output_device_id'},
    )

    updates, changed = audio_service._normalized_settings_patch(current, {"input_device_id": 3})
    assert (updates, changed) == ({'input_device_id': 3, 'input_device_name': 'New'}, {'input_device_id'})

    # output_device_id resolves output_device_name the same way input does --
    # a saved PortAudio index isn't a stable identity across a USB reconnect,
    # and only the input side used to be able to recover its device by name.
    updates, changed = audio_service._normalized_settings_patch(current, {"output_device_id": 5})
    assert (updates, changed) == (
        {'output_device_id': 5, 'output_device_name': 'New Speakers'}, {'output_device_id'}
    )

    raises(RuntimeError, lambda: audio_service._normalized_settings_patch(current, {'audio_driver': 'invalid'}), match='Unsupported')
    updates, changed = audio_service._normalized_settings_patch(current, {"audio_driver": "mme"})
    assert (updates, changed) == ({"audio_driver": "mme"}, {"audio_driver"})

    monkeypatch.setattr(audio_service, "list_asio_drivers", Mock(return_value=[]))
    raises(RuntimeError, lambda: audio_service._normalized_settings_patch(current, {'audio_driver': 'asio'}), match='no ASIO')

    patch_attrs(monkeypatch, audio_service, list_asio_drivers=Mock(return_value=['Studio ASIO', 'USB ASIO']))
    updates, changed = audio_service._normalized_settings_patch(
        current,
        {"audio_driver": "asio", "asio_driver_name": "missing"},
    )
    assert (updates['asio_driver_name'] == 'Studio ASIO') and ('asio_driver_name' in changed)


def test_selecting_windows_driver_clears_a_stale_asio_driver_name():
    current = settings(audio_driver="asio", asio_driver_name="Realtek ASIO")

    updates, changed = audio_service._normalized_settings_patch(
        current, {"audio_driver": "auto"}, resolve_devices=False
    )

    assert updates == {"audio_driver": "auto", "asio_driver_name": None}
    assert changed == {"audio_driver", "asio_driver_name"}


def test_update_settings_reconfigures_monitor_and_rolls_back(monkeypatch):
    current, database = settings(monitoring_enabled=True), Mock()
    monkeypatch.setattr(audio_service, "_get_or_create_settings", Mock(return_value=current))
    configure = Mock()
    monkeypatch.setattr(audio_service, "configure_monitoring", configure)

    result = audio_service.update_settings(database, {"buffer_size": 128})
    assert result is current and current.buffer_size == 128
    configure.assert_called_once_with(current)
    database.commit.assert_called_once_with()
    database.refresh.assert_called_once_with(current)

    database.reset_mock()
    configure.reset_mock()
    database.commit.side_effect = RuntimeError("database locked")
    raises(RuntimeError, lambda: audio_service.update_settings(database, {'buffer_size': 256}), match='locked')
    assert (current.buffer_size, configure.call_count) == (128, 2)
    database.rollback.assert_called_once_with()

    configure.side_effect = [None, RuntimeError("restore device failed")]
    raises(RuntimeError, lambda: audio_service.update_settings(database, {'buffer_size': 256}), match='locked')


def test_update_settings_sends_live_effect_update_instead_of_restarting(monkeypatch):
    current, database = settings(monitoring_enabled=True, reverb=0.1), Mock()
    monkeypatch.setattr(audio_service, "_get_or_create_settings", Mock(return_value=current))
    configure, live_update = Mock(), Mock()
    patch_attrs(monkeypatch, audio_service, configure_monitoring=configure, _send_live_update=live_update)

    result = audio_service.update_settings(database, {"reverb": 0.6, "echo": 0.3})
    assert result is current and current.reverb == 0.6 and current.echo == 0.3
    configure.assert_not_called()
    live_update.assert_called_once_with({"reverb": 0.6, "echo": 0.3})

    configure.reset_mock()
    live_update.reset_mock()
    asio_current = settings(monitoring_enabled=True, audio_driver="asio")
    monkeypatch.setattr(audio_service, "_get_or_create_settings", Mock(return_value=asio_current))
    audio_service.update_settings(database, {"echo": 0.4})
    configure.assert_called_once_with(asio_current)
    live_update.assert_not_called()

    configure.reset_mock()
    live_update.reset_mock()
    disabled_current = settings(monitoring_enabled=False)
    monkeypatch.setattr(audio_service, "_get_or_create_settings", Mock(return_value=disabled_current))
    audio_service.update_settings(database, {"delay": 0.5})
    configure.assert_not_called()
    live_update.assert_not_called()


def test_send_live_update_writes_json_line_and_tolerates_dead_or_missing_process(monkeypatch):
    process = Mock()
    process.poll.return_value = None
    monkeypatch.setattr(audio_service, "_monitor_process", process)
    audio_service._send_live_update({"reverb": 0.5})
    process.stdin.write.assert_called_once_with('{"reverb": 0.5}\n')
    process.stdin.flush.assert_called_once_with()

    process.stdin.write.side_effect = OSError("broken pipe")
    audio_service._send_live_update({"echo": 0.2})  # must not raise

    monkeypatch.setattr(audio_service, "_monitor_process", None)
    audio_service._send_live_update({"delay": 0.2})  # must not raise

    dead = Mock()
    dead.poll.return_value = 1
    monkeypatch.setattr(audio_service, "_monitor_process", dead)
    audio_service._send_live_update({"delay": 0.2})
    dead.stdin.write.assert_not_called()


def test_set_monitoring_enabled_is_idempotent_and_transactional(monkeypatch):
    database, current = Mock(), settings(monitoring_enabled=False)
    monkeypatch.setattr(audio_service, "get_settings", Mock(return_value=current))
    configure, stop = Mock(), Mock()
    patch_attrs(monkeypatch, audio_service, configure_monitoring=configure, stop_monitoring=stop)
    commit = Mock(side_effect=lambda _db, item: item)
    monkeypatch.setattr(audio_service, "commit_refresh", commit)

    assert audio_service.set_monitoring_enabled(database, False) is current
    stop.assert_called_once_with()
    assert (audio_service.set_monitoring_enabled(database, True) is current) and (current.monitoring_enabled is True)
    commit.assert_called_once_with(database, current)

    configure.reset_mock()
    assert audio_service.set_monitoring_enabled(database, True) is current
    configure.assert_called_once_with(current)

    current.monitoring_enabled = False
    configure.side_effect = [RuntimeError("device failed"), RuntimeError("restore failed")]
    raises(RuntimeError, lambda: audio_service.set_monitoring_enabled(database, True), match='device failed')
    assert current.monitoring_enabled is False
    database.rollback.assert_called_once_with()


def test_configure_monitoring_routes_auto_and_asio(monkeypatch):
    stop, asio, worker = Mock(), Mock(), Mock()
    patch_attrs(monkeypatch, audio_service, _stop_monitoring_process=stop, _start_asio_monitor=asio, _start_monitor_worker=worker, _monitor_effects_disabled=False)

    audio_service.configure_monitoring(settings(monitoring_enabled=False))
    stop.assert_called_once_with()
    asio.assert_not_called()

    audio_service.configure_monitoring(settings(monitoring_enabled=True, audio_driver="asio"))
    asio.assert_called_once()

    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", False)
    raises(RuntimeError, lambda: audio_service.configure_monitoring(settings(monitoring_enabled=True)), match='unavailable')

    patch_attrs(monkeypatch, audio_service, _AUDIO_BACKEND_AVAILABLE=True, preferred_input_device=Mock(return_value=1), preferred_output_device=Mock(return_value=2), _resolved_device_index=lambda value, _kind, _devices: value)
    devices = {
        1: {"name": "Selected microphone", "hostapi": 0, "default_samplerate": 48_000, "max_input_channels": 1},
        2: {"name": "Selected speakers", "hostapi": 0, "default_samplerate": 48_000, "max_output_channels": 2},
    }
    patch_many(
        monkeypatch,
        (audio_service.sd, "query_devices", lambda: devices),
        (audio_service.sd, "query_hostapis", lambda _index: {"name": "Windows WASAPI"}),
        (audio_service.sd, "check_input_settings", lambda **_kwargs: None),
        (audio_service.sd, "check_output_settings", lambda **_kwargs: None),
    )
    worker.reset_mock()
    audio_service.configure_monitoring(settings(monitoring_enabled=True, volume=8, buffer_size=128))
    worker_options = dict(worker.call_args.args[0])
    # Plain monitoring never asks for a relay (relay_needed defaults to
    # False/unset here) -- no port key, no relay opened at all.
    assert "audio_relay_port" not in worker_options
    assert audio_service._monitor_relay is None
    assert worker_options == {
        "input_device_id": 1,
        "output_device_id": 2,
        "sample_rate": 48_000.0,
        "output_channels": 2,
        "blocksize": 128,
        "gain": 4,
        "reverb": 0.0,
        "echo": 0.0,
                "delay": 0.0,
                "octave": 0.0,
                "noise_suppression": 0.35,
                "dry_monitor": 0.0,
            "wasapi_mode": "shared",
            "native_shared": True,
            "input_device_name": "Selected microphone",
            "output_device_name": "Selected speakers",
    }

    monkeypatch.setattr(audio_service, "_monitor_effects_disabled", True)
    audio_service.configure_monitoring(
        settings(monitoring_enabled=True, reverb=0.8, echo=0.7, delay=0.6)
    )
    disabled = worker.call_args.args[0]
    # "No effects" monitoring has nothing left for the Python DSP chain to
    # do, so it arms the native raw pass-through from the first block
    # instead of only after a later live update.
    assert (disabled["reverb"], disabled["echo"], disabled["delay"]) == (0.0, 0.0, 0.0)
    assert disabled["noise_suppression"] == 0.0
    assert disabled["dry_monitor"] == 1.0

    devices[2]["max_output_channels"] = 0
    raises(RuntimeError, lambda: audio_service.configure_monitoring(settings(monitoring_enabled=True)), match='No output')


def test_asio_monitor_validates_bridge_driver_and_clamps_command(monkeypatch, tmp_path):
    bridge = tmp_path / "bridge.exe"
    monkeypatch.setattr(audio_service, "_asio_bridge_path", Mock(return_value=bridge))
    raises(RuntimeError, lambda: audio_service._start_asio_monitor(settings(audio_driver='asio')), match='not built')

    bridge.write_bytes(b"bridge")
    monkeypatch.setattr(audio_service, "list_asio_drivers", Mock(return_value=["Studio ASIO"]))
    raises(RuntimeError, lambda: audio_service._start_asio_monitor(settings(audio_driver='asio', asio_driver_name='Missing')), match='unavailable')

    launch = Mock()
    monkeypatch.setattr(audio_service, "_launch_monitor_process", launch)
    audio_service._start_asio_monitor(
        settings(
            audio_driver="asio",
            asio_driver_name="Studio ASIO",
            volume=9,
            reverb=2,
            echo=-1,
            delay=0.5,
        )
    )
    command = launch.call_args.args[0]
    assert (command[command.index('--gain') + 1] == '4.0') and (command[command.index('--reverb') + 1] == '1.0') and (command[command.index('--echo') + 1] == '0.0') and (command[command.index('--octave') + 1] == '0.0')
    assert command[command.index('--noise-suppression') + 1] == '0.35'
    assert launch.call_args.kwargs["cwd"] == tmp_path
    assert callable(launch.call_args.kwargs["on_driver_reset"])
    assert launch.call_args.kwargs["on_buffer_negotiated"] is None

    monkeypatch.setattr(audio_service, "_monitor_effects_disabled", True)
    audio_service._start_asio_monitor(
        settings(audio_driver="asio", asio_driver_name="Studio ASIO", noise_suppression=0.8)
    )
    disabled_command = launch.call_args.args[0]
    assert disabled_command[disabled_command.index('--noise-suppression') + 1] == '0.0'


def test_automatic_asio_matches_the_selected_windows_hardware_not_a_generic_driver():
    drivers = [
        "Generic Low Latency ASIO Driver",
        "Audient USB Audio ASIO Driver",
        "Unrelated Studio ASIO",
    ]

    assert audio_service._matching_automatic_asio_drivers(
        drivers,
        "Analogue 1/2 (2- Audient iD14)",
        "Analogue 3/4 (2- Audient iD14)",
    ) == ["Audient USB Audio ASIO Driver"]
    assert audio_service._matching_automatic_asio_drivers(
        drivers,
        "Microphone (Realtek(R) Audio)",
        "Speakers (Realtek(R) Audio)",
    ) == []
    assert audio_service._matching_automatic_asio_drivers(
        ["Generic Low Latency ASIO Driver", "Realtek ASIO"],
        "Microphone (Realtek(R) Audio)",
        "Speakers (Realtek(R) Audio)",
    ) == ["Realtek ASIO"]


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("Analogue 1/2 (2- Audient iD14)", 0),
        ("Analogue 3/4 (2- Audient iD14)", 2),
        ("Line Out 7-8", 6),
        ("Speakers (Realtek(R) Audio)", None),
    ],
)
def test_asio_channel_base_follows_the_selected_windows_endpoint_pair(endpoint, expected):
    assert audio_service._asio_channel_base(endpoint) == expected


def test_windows_driver_keeps_verified_matching_asio_as_its_low_latency_transport(
    monkeypatch,
):
    from app.services import recording_service

    current = settings(
        audio_driver="auto",
        monitoring_enabled=True,
        input_device_name="Microphone (Realtek Audio)",
        output_device_name="Speakers (Realtek Audio)",
    )
    monkeypatch.setattr(recording_service, "apply_monitor_settings", lambda *_: False)
    devices = []
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service.sd, "query_devices", Mock(return_value=devices))
    automatic = Mock(return_value=True)
    shared = Mock()
    monkeypatch.setattr(audio_service, "_try_automatic_asio_monitor", automatic)
    monkeypatch.setattr(audio_service, "_start_shared_monitor", shared)

    audio_service.configure_monitoring(current)

    automatic.assert_called_once_with(current, devices=devices)
    shared.assert_not_called()


def test_windows_driver_falls_back_to_shared_when_no_safe_asio_transport_exists(
    monkeypatch,
):
    from app.services import recording_service

    current = settings(audio_driver="auto", monitoring_enabled=True)
    monkeypatch.setattr(recording_service, "apply_monitor_settings", lambda *_: False)
    devices = []
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service.sd, "query_devices", Mock(return_value=devices))
    monkeypatch.setattr(audio_service, "_try_automatic_asio_monitor", Mock(return_value=False))
    shared = Mock()
    monkeypatch.setattr(audio_service, "_start_shared_monitor", shared)

    audio_service.configure_monitoring(current)

    shared.assert_called_once_with(
        current, driver="auto", relay_needed=False, devices=devices
    )


def test_asio_reset_restart_closure_holds_a_detached_snapshot_not_the_live_row(monkeypatch, tmp_path):
    # configure_monitoring can be called with a live, DB-session-bound
    # AudioSettings row (see app/routers/recording.py) whose session may
    # already be closed by the time a driver reset happens later -- the
    # restart closure must capture its own plain snapshot up front rather
    # than close over that row.
    bridge = tmp_path / "bridge.exe"
    bridge.write_bytes(b"bridge")
    monkeypatch.setattr(audio_service, "_asio_bridge_path", Mock(return_value=bridge))
    monkeypatch.setattr(audio_service, "list_asio_drivers", Mock(return_value=["Studio ASIO"]))
    launch = Mock()
    monkeypatch.setattr(audio_service, "_launch_monitor_process", launch)
    live_row = settings(audio_driver="asio", asio_driver_name="Studio ASIO", buffer_size=256)

    audio_service._start_asio_monitor(live_row)
    on_driver_reset = launch.call_args.kwargs["on_driver_reset"]

    request = Mock()
    monkeypatch.setattr(audio_service, "request_monitoring", request)
    on_driver_reset()

    request.assert_called_once()
    (resubmitted,), kwargs = request.call_args
    assert resubmitted is not live_row
    assert resubmitted.buffer_size == 256
    assert resubmitted.asio_driver_name == "Studio ASIO"
    assert kwargs.get("adopt_driver_buffer") is True


def test_asio_monitor_adopts_the_drivers_own_buffer_instead_of_the_saved_one(monkeypatch, tmp_path):
    # A driver-initiated reset (see the test above) means the driver's own
    # control panel changed something -- re-asserting this app's last-saved
    # buffer_size would just clamp straight back to it and the panel change
    # would never actually take effect. "0" is the bridge's sentinel for
    # "use whatever the driver currently prefers" (resolve_buffer_size in
    # bridge_main.cpp).
    bridge = tmp_path / "bridge.exe"
    bridge.write_bytes(b"bridge")
    monkeypatch.setattr(audio_service, "_asio_bridge_path", Mock(return_value=bridge))
    monkeypatch.setattr(audio_service, "list_asio_drivers", Mock(return_value=["Studio ASIO"]))
    launch = Mock()
    monkeypatch.setattr(audio_service, "_launch_monitor_process", launch)

    audio_service._start_asio_monitor(
        settings(audio_driver="asio", asio_driver_name="Studio ASIO", buffer_size=64),
        adopt_driver_buffer=True,
    )

    command = launch.call_args.args[0]
    assert command[command.index("--buffer-size") + 1] == "0"
    assert launch.call_args.kwargs["on_buffer_negotiated"] is audio_service._persist_negotiated_buffer_size


def test_persist_negotiated_buffer_size_writes_only_when_it_actually_changed(monkeypatch):
    import database

    current = settings(buffer_size=64)
    db = Mock()
    monkeypatch.setattr(database, "SessionLocal", Mock(return_value=db))
    monkeypatch.setattr(audio_service, "_get_or_create_settings", Mock(return_value=current))
    commit = Mock()
    monkeypatch.setattr(audio_service, "commit_refresh", commit)

    audio_service._persist_negotiated_buffer_size(64)
    commit.assert_not_called()
    db.close.assert_called_once_with()

    db.close.reset_mock()
    audio_service._persist_negotiated_buffer_size(256)
    commit.assert_called_once_with(db, current)
    assert current.buffer_size == 256
    db.close.assert_called_once_with()


def test_asio_bridge_converts_supported_mismatched_input_and_output_formats():
    source = (Path(__file__).parents[1] / "engines/asio/bridge_main.cpp").read_text(encoding="utf-8")
    assert "use_dsp && supports_dsp(output_type)" in source
    assert "decode_sample(input, input_type, index) * g_engine.gain" in source
    assert "output_type == input_type && use_dsp" not in source
    assert "options.input_channel < 0" in source
    assert "sample_peak(candidate" in source


def test_asio_pitch_effect_uses_the_same_low_latency_window_as_windows_monitor():
    source = (Path(__file__).parents[1] / "engines/asio/bridge_main.cpp").read_text(encoding="utf-8")

    assert "kPitchWindowSeconds = 0.012" in source
    assert "rate * kPitchWindowSeconds" in source
    assert "rate * 0.032" not in source


def test_signal_quality_uses_monitor_or_direct_capture(monkeypatch):
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", False)
    raises(RuntimeError, lambda: audio_service.check_signal_quality(None), match='недоступен')

    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    live = Mock()
    live.poll.return_value = None
    patch_attrs(monkeypatch, audio_service, _monitor_process=live, _monitor_signal={'rms_db': -12.0, 'clipping': False, 'silent': False})
    assert audio_service.check_signal_quality(None)["rms_db"] == -12

    dead, dead_reader = Mock(), Mock()
    dead.poll.return_value = 1
    patch_attrs(monkeypatch, audio_service, _monitor_process=dead, _monitor_reader=dead_reader)
    assert (audio_service.check_signal_quality(None, monitoring_expected=True)['silent'] is True) and (audio_service._monitor_process is None)
    # A worker discovered dead here (poll() != None) has nothing else to
    # close its pipes/join its reader -- unlike the ordinary stop path, this
    # is the only place that ever notices it died, so it must clean up
    # itself instead of just dropping the _monitor_process reference.
    dead.stdout.close.assert_called_once_with()
    dead.stdin.close.assert_called_once_with()
    dead_reader.join.assert_called_once_with(timeout=1.0)
    assert audio_service._monitor_reader is None

    monkeypatch.setattr(audio_service, "_monitor_process", None)
    patch_attrs(monkeypatch, audio_service.sd, rec=Mock(return_value=np.array([[0.5], [-0.5]], dtype=np.float32)), wait=Mock())
    quality = audio_service.check_signal_quality(2, gain=2, duration_sec=0.1)
    assert quality == {"rms_db": 0.0, "clipping": True, "silent": False}

    audio_service.sd.rec.return_value = np.empty((0, 1), dtype=np.float32)
    assert audio_service.check_signal_quality(None)["rms_db"] == -120


def test_signal_quality_cache_is_invalidated_by_a_gain_change(monkeypatch):
    # The cached result is computed from samples * gain -- reusing it across
    # a gain change (still within the cache window) silently kept showing
    # the level for whatever gain was active when the probe last actually
    # ran, for up to _SIGNAL_PROBE_INTERVAL_SEC.
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service, "_monitor_process", None)
    patch_attrs(
        monkeypatch, audio_service.sd,
        rec=Mock(return_value=np.array([[0.25], [-0.25]], dtype=np.float32)), wait=Mock(),
    )

    low = audio_service.check_signal_quality(3, gain=1.0, duration_sec=0.1)
    high = audio_service.check_signal_quality(3, gain=4.0, duration_sec=0.1)
    assert low["rms_db"] < high["rms_db"]
    assert high["clipping"] is True

    # Same device AND gain, still within the cache window: no new probe.
    audio_service.sd.rec.side_effect = AssertionError("must reuse the cached result")
    assert audio_service.check_signal_quality(3, gain=4.0, duration_sec=0.1) == high


def test_signal_quality_uses_selected_device_native_sample_rate(monkeypatch):
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    monkeypatch.setattr(audio_service, "_monitor_process", None)
    devices = [{"max_input_channels": 1, "default_samplerate": 16_000}]
    monkeypatch.setattr(audio_service.sd, "query_devices", Mock(return_value=devices))
    monkeypatch.setattr(audio_service.sd, "default", Mock(device=(0, 0)))

    def record(_frames, *, samplerate, **_kwargs):
        if samplerate != 16_000:
            raise RuntimeError("Invalid sample rate")
        return np.array([[0.25], [-0.25]], dtype=np.float32)

    monkeypatch.setattr(audio_service.sd, "rec", Mock(side_effect=record))
    monkeypatch.setattr(audio_service.sd, "wait", Mock())

    quality = audio_service.check_signal_quality(0, duration_sec=0.1)

    assert not quality["silent"]
    assert audio_service.sd.rec.call_args.kwargs["samplerate"] == 16_000


def test_audio_backend_import_failure_is_soft(monkeypatch):
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name in {"numpy", "sounddevice"}: raise ImportError("audio stack missing")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", blocked_import)
        reloaded = importlib.reload(audio_service)
        assert reloaded._AUDIO_BACKEND_AVAILABLE is False
    importlib.reload(audio_service)
