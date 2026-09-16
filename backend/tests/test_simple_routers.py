import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

import schemas
from app import main
from app.routers import application, cache, diagnostics, player
from tests._shared import assert_http_status, patch_attrs, patch_many


def test_application_router_translates_service_validation(monkeypatch):
    patch_attrs(monkeypatch, application.app_settings_service, read_settings=Mock(return_value={'x': 1}), read_ui_preferences=Mock(return_value={'karaoke': {'speed': 1}}))
    assert (application.get_settings() == {'x': 1}) and (application.get_ui_preferences() == {'karaoke': {'speed': 1}})

    update = Mock(return_value={"theme": "dark"})
    monkeypatch.setattr(application.app_settings_service, "update_settings", update)
    assert application.update_settings(application.AppSettingsPatch(theme="dark")) == {
        "theme": "dark"
    }
    update.side_effect = ValueError("invalid path")
    assert_http_status(422, lambda: application.update_settings(application.AppSettingsPatch(theme="dark")))

    preferences = Mock(return_value={"speed": 1})
    monkeypatch.setattr(application.app_settings_service, "update_ui_preferences", preferences)
    assert application.update_ui_preferences("karaoke", {"speed": 1}) == {"speed": 1}
    preferences.side_effect = ValueError("unknown")
    with pytest.raises(HTTPException) as unknown: application.update_ui_preferences("unknown", {})
    assert (unknown.value.status_code == 422) and (application.about()['name'] == 'A&D Voice')

    preview = {"enabled": False, "errors": False}
    delete = Mock(return_value=True)
    patch_attrs(
        monkeypatch,
        application.remote_log_service,
        diagnostics_policy_preview=Mock(return_value=preview),
        delete_remote_diagnostics=delete,
    )
    update.side_effect = None
    update.return_value = {}
    assert application.remote_diagnostics_policy() == preview
    assert application.delete_remote_diagnostics() == {"deleted": True, "settings": {}}
    delete.assert_called_once_with()
    assert update.call_args.args[0] == {
        "remote_diagnostics_enabled": False,
        "remote_diagnostics_errors_enabled": False,
        "remote_diagnostics_hardware_enabled": False,
        "remote_crash_reports_enabled": False,
    }


def test_application_history_merges_and_sorts_processing_and_recordings():
    songs = [
        SimpleNamespace(
            title="Processing",
            status=SimpleNamespace(value="processing"),
            updated_at=None,
        )
    ]
    recording, recording_query = SimpleNamespace(duration_sec=12.5, created_at=SimpleNamespace(isoformat=lambda: '2026-01-02T00:00:00')), Mock()
    recording_query.join.return_value.outerjoin.return_value.order_by.return_value.limit.return_value.all.return_value = [
        (recording, "Recorded", "analysis"),
        (SimpleNamespace(duration_sec=None, created_at=None), "Raw", None),
    ]
    database = Mock()
    database.scalars.return_value = songs
    database.query.return_value = recording_query

    history = application.get_history(database)

    assert ([item['song_title'] for item in history], history[0]['status'], history[-1]['status']) == (['Recorded', 'Processing', 'Raw'], 'analyzed', 'recorded')


def test_player_router_forwards_song_identity_and_payload(monkeypatch):
    song, database, calls = SimpleNamespace(id='song'), Mock(), {'get_sync_data': {'sync': True}, 'get_timeline': {'timeline': True}, 'get_state': 'state', 'seek': 'seek', 'set_playing': 'playing', 'stop': 'stopped'}
    for name, result in calls.items(): monkeypatch.setattr(player.player_service, name, Mock(return_value=result))
    media = Mock()
    monkeypatch.setattr(player.audio_service, "set_shared_media_active", media)
    assert (player.get_sync(song) == {'sync': True}) and (player.get_timeline(song) == {'timeline': True}) and (player.get_position(song, database) == 'state') and (player.seek(song, schemas.SeekRequest(position_sec=2.5), database) == 'seek') and (player.pause(song, database) == 'playing')
    player.player_service.set_playing.assert_called_with(database, "song", False)
    assert player.resume(song, database) == "playing"
    player.player_service.set_playing.assert_called_with(database, "song", True)
    assert player.stop(song, database) == "stopped"
    assert [item.args for item in media.call_args_list] == [
        (database, "karaoke:song", False),
        (database, "karaoke:song", True),
        (database, "karaoke:song", False),
    ]


def test_karaoke_playback_continues_if_monitor_reconfiguration_fails(monkeypatch):
    song, database = SimpleNamespace(id="song"), Mock()
    monkeypatch.setattr(player.audio_service, "set_shared_media_active", Mock(side_effect=RuntimeError("audio busy")))
    monkeypatch.setattr(player.player_service, "set_playing", Mock(return_value="playing"))
    assert player.resume(song, database) == "playing"


def test_diagnostics_router_forwards_all_services(monkeypatch):
    health = {
        "ffmpeg_available": True,
        "demucs_available": True,
        "whisper_available": True,
        "torch_available": True,
        "cuda_available": False,
        "ai_dir_found": True,
        "runtime": {"selected": {"pitch": {"backend": "pytorch", "device": "cpu"}}},
    }
    patch_attrs(monkeypatch, diagnostics.diagnostics_service, pipeline_health=Mock(return_value=health), versions=Mock(return_value={'v': 1}), recent_errors=Mock(return_value=[{'e': 1}]))
    patch_attrs(monkeypatch, diagnostics.model_install_service, status=Mock(return_value={'s': 1}), start_download=Mock(return_value={'s': 2}))
    monkeypatch.setattr(diagnostics.background_task_supervisor, "snapshot", Mock(return_value={'tasks': []}))
    monkeypatch.setattr(diagnostics.storage_budget_service, "snapshot", Mock(return_value={'count': 0}))
    monkeypatch.setattr(diagnostics.startup_service, "snapshot", Mock(return_value={'ready': True, 'status': 'ready'}))
    monkeypatch.setattr(diagnostics.database, "schema_status", Mock(return_value={'current': 1}))
    assert (diagnostics.health()['status'] == 'ok') and (diagnostics.pipeline_health() == health) and (diagnostics.models_health() == health) and (diagnostics.ai_models_status() == {'s': 1}) and (diagnostics.download_ai_models() == {'s': 2}) and (diagnostics.versions() == {'v': 1}) and (diagnostics.errors() == {'errors': [{'e': 1}]}) and (diagnostics.background_tasks() == {'tasks': [], 'storage': {'count': 0}}) and (diagnostics.database_schema() == {'current': 1})


def test_cache_router_success_and_empty_optimization(monkeypatch):
    patch_attrs(monkeypatch, cache.cache_service, clear_temp_files=Mock(return_value=12), cache_size=Mock(return_value={'size': 1}), free_space=Mock(return_value={'free': 2}))
    assert (cache.clear_cache() == {'freed_bytes': 12}) and (cache.delete_temp() == {'freed_bytes': 12}) and (cache.get_cache_size() == {'size': 1}) and (cache.get_free_space() == {'free': 2})

    optimize = Mock(return_value={"actions": ["done"], "freed_bytes": 1})
    monkeypatch.setattr(cache.cache_service, "optimize_song_files", optimize)
    assert cache.optimize_song("song")["actions"] == ["done"]
    optimize.return_value = {"actions": [], "freed_bytes": 0}
    assert_http_status(404, lambda: cache.optimize_song("song"))


def test_benign_disconnect_requires_known_windows_socket_code():
    error = ConnectionResetError()
    error.winerror = 10054
    assert main._is_benign_client_disconnect({"exception": error}) is True
    error.winerror = 1
    assert (main._is_benign_client_disconnect({'exception': error}) is False) and (main._is_benign_client_disconnect({}) is False)


def test_lifespan_installs_handler_initializes_and_always_cleans(monkeypatch):
    loop, previous = Mock(), Mock()
    loop.get_exception_handler.return_value = previous
    monkeypatch.setattr(main.asyncio, "get_running_loop", Mock(return_value=loop))
    initialize, start_startup, close, stop, reset_ai = Mock(), Mock(), Mock(side_effect=RuntimeError('close failed')), Mock(), Mock()
    monkeypatch.setattr(main, "init_db", initialize)
    shutdown_processing = Mock()
    start_background, stop_background, shutdown_background = Mock(), Mock(), Mock()
    patch_many(monkeypatch, (main.startup_service, "start", start_startup), (main.pipeline_service, "shutdown_active_processing", shutdown_processing), (main.background_task_supervisor, "start_accepting", start_background), (main.background_task_supervisor, "stop_accepting", stop_background), (main.background_task_supervisor, "shutdown", shutdown_background), (main.recording_service, "close_all_sessions", close), (main.audio_service, "stop_monitoring", stop), (main.ai_service, "reset_ai_service", reset_ai))

    async def exercise():
        with pytest.raises(RuntimeError, match="close failed"):
            async with main.lifespan(main.app): pass

    asyncio.run(exercise())
    initialize.assert_called_once_with()
    start_startup.assert_called_once_with()
    start_background.assert_called_once_with()
    stop_background.assert_called_once_with()
    shutdown_processing.assert_called_once_with(timeout=15.0)
    shutdown_background.assert_called_once_with(timeout=10.0)
    stop.assert_called_once_with()
    reset_ai.assert_called_once_with()
    assert loop.set_exception_handler.call_args_list[-1].args == (previous,)

    handler, benign = loop.set_exception_handler.call_args_list[0].args[0], ConnectionResetError()
    benign.winerror = 64
    handler(loop, {"exception": benign})
    previous.assert_not_called()
    handler(loop, {"exception": RuntimeError("bad")})
    previous.assert_called_once()


def test_lifespan_handler_uses_loop_default_without_previous(monkeypatch):
    loop = Mock()
    loop.get_exception_handler.return_value = None
    patch_many(monkeypatch, (main.asyncio, "get_running_loop", Mock(return_value=loop)), (main, "init_db", Mock()), (main.startup_service, "start", Mock()), (main.pipeline_service, "shutdown_active_processing", Mock()), (main.background_task_supervisor, "start_accepting", Mock()), (main.background_task_supervisor, "stop_accepting", Mock()), (main.background_task_supervisor, "shutdown", Mock()), (main.recording_service, "close_all_sessions", Mock()), (main.audio_service, "stop_monitoring", Mock()), (main.ai_service, "reset_ai_service", Mock()))

    async def exercise():
        async with main.lifespan(main.app):
            handler = loop.set_exception_handler.call_args.args[0]
            handler(loop, {"exception": RuntimeError("bad")})

    asyncio.run(exercise())
    loop.default_exception_handler.assert_called_once()
    assert main.root() == {"name": "A&D Voice Backend", "docs": "/docs"}
