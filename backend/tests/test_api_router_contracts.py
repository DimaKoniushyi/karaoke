from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

from sqlalchemy.exc import IntegrityError

import models
import schemas
from app.routers import analysis, audio
from tests._shared import assert_http_status, patch_attrs, raises


def test_audio_router_forwards_devices_settings_and_selection(monkeypatch):
    database = Mock()
    patch_attrs(monkeypatch, audio.audio_service, list_input_devices=Mock(return_value=[{'i': 1}]), list_output_devices=Mock(return_value=[{'o': 1}]), list_asio_drivers=Mock(return_value=['ASIO']))
    current = SimpleNamespace(input_device_id=2, volume=1, monitoring_enabled=False)
    monkeypatch.setattr(audio.audio_service, "get_settings", Mock(return_value=current))
    update = Mock(return_value=current)
    monkeypatch.setattr(audio.audio_service, "update_settings", update)
    monitor = Mock(return_value=current)
    monkeypatch.setattr(audio.audio_service, "set_monitoring_enabled", monitor)

    assert (audio.list_devices() == [{'i': 1}]) and (audio.list_output_devices() == [{'o': 1}]) and (audio.list_asio_drivers() == [{'name': 'ASIO'}]) and (audio.get_settings(database) is current) and (audio.update_settings(schemas.AudioSettingsUpdate(volume=2), database) is current)
    update.assert_called_with(database, {"volume": 2.0}, background=True)
    assert (audio.start_direct_monitoring(database) is current) and (audio.stop_direct_monitoring(database) is current) and (audio.select_device(4, database) is current)
    assert audio.start_direct_monitoring(database, disabled_effects=True) is current
    monitor.assert_any_call(database, True, disabled_effects=True, background=True)
    update.assert_called_with(database, {"input_device_id": 4}, background=True)


def test_audio_router_translates_runtime_failures(monkeypatch):
    database = Mock()
    patch_attrs(monkeypatch, audio.audio_service, update_settings=Mock(side_effect=RuntimeError('device failed')))
    assert_http_status(503, lambda: audio.update_settings(schemas.AudioSettingsUpdate(volume=2), database))

    patch_attrs(monkeypatch, audio.audio_service, set_monitoring_enabled=Mock(side_effect=RuntimeError('monitor failed')))
    assert_http_status(503, lambda: audio.start_direct_monitoring(database))


def test_signal_quality_uses_persisted_gain_and_monitor_state(monkeypatch):
    database, current = Mock(), SimpleNamespace(
        input_device_id=2, volume=1.5, monitoring_enabled=True, audio_driver="auto", asio_driver_name=None
    )
    monkeypatch.setattr(audio.audio_service, "get_settings", Mock(return_value=current))
    resolve = Mock(return_value=2)
    monkeypatch.setattr(audio.audio_service, "preferred_input_device", resolve)
    check = Mock(return_value={"rms_db": -10})
    monkeypatch.setattr(audio.audio_service, "check_signal_quality", check)
    assert audio.signal_quality(database) == {"rms_db": -10}
    resolve.assert_called_once_with(2, "auto", None, device_name=None)
    check.assert_called_once_with(2, gain=1.5, monitoring_expected=True)
    check.side_effect = RuntimeError("unavailable")
    assert_http_status(503, lambda: audio.signal_quality(database))


def test_signal_quality_skips_device_resolution_while_monitoring_is_live(monkeypatch):
    # Settings polls this endpoint every 80ms while monitoring is on;
    # check_signal_quality answers from the live worker's own cached reports
    # in that case and never looks at device_id at all, so resolving one
    # first (a full PortAudio device-table enumeration) would be pure waste
    # on every single poll for as long as the page stays open.
    database, current = Mock(), SimpleNamespace(
        input_device_id=2, volume=1.5, monitoring_enabled=True, audio_driver="auto", asio_driver_name=None
    )
    monkeypatch.setattr(audio.audio_service, "get_settings", Mock(return_value=current))
    monkeypatch.setattr(audio.audio_service, "is_monitor_process_alive", Mock(return_value=True))
    resolve = Mock(return_value=2)
    monkeypatch.setattr(audio.audio_service, "preferred_input_device", resolve)
    check = Mock(return_value={"rms_db": -10})
    monkeypatch.setattr(audio.audio_service, "check_signal_quality", check)

    assert audio.signal_quality(database) == {"rms_db": -10}

    resolve.assert_not_called()
    check.assert_called_once_with(None, gain=1.5, monitoring_expected=True)


def test_signal_quality_resolves_the_matching_asio_input_not_the_saved_raw_id(monkeypatch):
    # A saved input_device_id is a PortAudio index for whatever driver was
    # selected when it was stored -- with an ASIO driver active it must be
    # re-resolved to the matching ASIO device, exactly like monitoring and
    # recording already do, not probed as-is (which would silently read the
    # wrong physical input, or Windows' default WASAPI/MME device).
    database, current = Mock(), SimpleNamespace(
        input_device_id=None, volume=1.0, monitoring_enabled=False, audio_driver="asio", asio_driver_name="Focusrite USB ASIO"
    )
    monkeypatch.setattr(audio.audio_service, "get_settings", Mock(return_value=current))
    resolve = Mock(return_value=7)
    monkeypatch.setattr(audio.audio_service, "preferred_input_device", resolve)
    check = Mock(return_value={"rms_db": -30})
    monkeypatch.setattr(audio.audio_service, "check_signal_quality", check)

    assert audio.signal_quality(database) == {"rms_db": -30}

    resolve.assert_called_once_with(None, "asio", "Focusrite USB ASIO", device_name=None)
    check.assert_called_once_with(7, gain=1.0, monitoring_expected=False)


def analysis_result(**changes):
    values = {
        "id": "analysis",
        "recording_id": "recording",
        "pitch_accuracy_percent": 90,
        "mean_deviation_semitones": 0.2,
        "rhythm_accuracy_percent": 80,
        "note_hold_percent": 95,
        "note_coverage_percent": 85,
        "overall_score_percent": 88,
        "sections_json": '[{"label":"verse"}]',
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(changes)
    return models.AnalysisResult(**values)


def test_analysis_output_and_read_endpoints():
    result = analysis_result()
    output = analysis._to_out(result)
    assert output.overall_score_percent == 88
    assert (output.sections == [{'label': 'verse'}]) and (analysis.get_analysis(result) == output) and (analysis.get_accuracy(result) == {'pitch_accuracy_percent': 90}) and (analysis.get_deviation(result) == {'mean_deviation_semitones': 0.2}) and (analysis.get_sections(result) == {'sections': [{'label': 'verse'}]})


def test_run_analysis_handles_missing_song_existing_and_domain_failure(monkeypatch):
    recording, database = SimpleNamespace(id='recording', song_id='song'), Mock()
    monkeypatch.setattr(analysis.repositories, "get_song", Mock(return_value=None))
    assert_http_status(404, lambda: analysis.run_analysis(recording, database))

    song = object()
    analysis.repositories.get_song.return_value = song
    existing = analysis_result()
    patch_attrs(monkeypatch, analysis.repositories, get_analysis_by_recording=Mock(return_value=existing))
    assert analysis.run_analysis(recording, database).id == "analysis"

    analysis.repositories.get_analysis_by_recording.return_value = None
    patch_attrs(monkeypatch, analysis.analysis_service, analyze_recording=Mock(side_effect=ValueError('reference missing')))
    assert_http_status(409, lambda: analysis.run_analysis(recording, database))


def test_run_analysis_persists_result_and_recovers_insert_race(monkeypatch):
    recording, database = SimpleNamespace(id='recording', song_id='song'), Mock()
    monkeypatch.setattr(analysis.repositories, "get_song", Mock(return_value=object()))
    lookup = Mock(return_value=None)
    monkeypatch.setattr(analysis.repositories, "get_analysis_by_recording", lookup)
    patch_attrs(monkeypatch, analysis.analysis_service, analyze_recording=Mock(return_value={'pitch_accuracy_percent': 80, 'mean_deviation_semitones': 0.4, 'sections': [{'label': 'verse'}]}))
    database.refresh.side_effect = lambda result: (
        setattr(result, "id", "new"),
        setattr(result, "created_at", datetime(2026, 1, 1, tzinfo=UTC)),
    )
    assert analysis.run_analysis(recording, database).id == "new"

    database.reset_mock()
    database.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    stored = analysis_result(id="winner")
    lookup.side_effect = [None, stored]
    assert analysis.run_analysis(recording, database).id == "winner"
    database.rollback.assert_called_once_with()


def test_run_analysis_reraises_database_failures(monkeypatch):
    recording, database = SimpleNamespace(id='recording', song_id='song'), Mock()
    monkeypatch.setattr(analysis.repositories, "get_song", Mock(return_value=object()))
    lookup = Mock(return_value=None)
    monkeypatch.setattr(analysis.repositories, "get_analysis_by_recording", lookup)
    patch_attrs(monkeypatch, analysis.analysis_service, analyze_recording=Mock(return_value={'pitch_accuracy_percent': None, 'mean_deviation_semitones': None, 'sections': None}))
    database.commit.side_effect = IntegrityError("insert", {}, Exception("duplicate"))
    raises(IntegrityError, lambda: analysis.run_analysis(recording, database))
    database.rollback.assert_called_once_with()

    database.reset_mock()
    database.commit.side_effect = RuntimeError("database unavailable")
    raises(RuntimeError, lambda: analysis.run_analysis(recording, database), match='unavailable')
    database.rollback.assert_called_once_with()
