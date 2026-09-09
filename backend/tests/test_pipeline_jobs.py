import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import models
from app.services import pipeline_service
from app.utils.json_files import write_json
from tests._shared import mock_song_lookup, patch_attrs, patch_many, raises, write_audio


def _reset_processing_slots(monkeypatch, *, limit):
    monkeypatch.setattr(pipeline_service, "_processing_queue", [])
    monkeypatch.setattr(pipeline_service, "_processing_active", set())
    monkeypatch.setattr(pipeline_service, "_cancelled_jobs", set())
    monkeypatch.setattr(pipeline_service.ai_bridge, "max_concurrent_jobs", Mock(return_value=limit))


def test_acquire_processing_slot_allows_up_to_the_configured_limit_at_once(monkeypatch):
    _reset_processing_slots(monkeypatch, limit=2)
    assert pipeline_service._acquire_processing_slot("a") is True
    assert pipeline_service._acquire_processing_slot("b") is True
    assert pipeline_service._processing_active == {"a", "b"}

    # A job beyond the limit must block until a slot frees, not run
    # immediately alongside the two already-active ones.
    result = {}
    started = threading.Event()

    def waiter():
        started.set()
        result["acquired"] = pipeline_service._acquire_processing_slot("c")

    thread = threading.Thread(target=waiter)
    thread.start()
    assert started.wait(timeout=2)
    time.sleep(0.1)
    assert thread.is_alive() and "c" not in pipeline_service._processing_active

    pipeline_service._release_processing_slot("a")
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result["acquired"] is True
    assert pipeline_service._processing_active == {"b", "c"}

    pipeline_service._release_processing_slot("b")
    pipeline_service._release_processing_slot("c")
    assert pipeline_service._processing_active == set()


def test_release_processing_slot_restores_priority_only_once_the_queue_is_idle(monkeypatch):
    # _configure_ai_runtime() lowers the whole backend process's priority
    # (there's no separate AI worker process) -- it must not be restored
    # while another queued/active song is still relying on that background
    # throttling, only once nothing is left processing.
    _reset_processing_slots(monkeypatch, limit=1)
    restore = Mock()
    monkeypatch.setattr(pipeline_service, "_restore_process_priority", restore)

    assert pipeline_service._acquire_processing_slot("a") is True
    pipeline_service._processing_queue.append("b")

    pipeline_service._release_processing_slot("a")
    restore.assert_not_called()

    pipeline_service._processing_queue.remove("b")
    pipeline_service._release_processing_slot("b")
    restore.assert_called_once()


def test_acquire_processing_slot_admits_queued_waiters_in_fifo_order(monkeypatch):
    _reset_processing_slots(monkeypatch, limit=1)
    assert pipeline_service._acquire_processing_slot("a") is True

    order = []

    def waiter(song_id, ready):
        ready.set()
        pipeline_service._acquire_processing_slot(song_id)
        order.append(song_id)

    ready_b, ready_c = threading.Event(), threading.Event()
    thread_b = threading.Thread(target=waiter, args=("b", ready_b))
    thread_b.start()
    assert ready_b.wait(timeout=2)
    time.sleep(0.05)  # let b actually enqueue itself before c starts
    thread_c = threading.Thread(target=waiter, args=("c", ready_c))
    thread_c.start()
    assert ready_c.wait(timeout=2)
    time.sleep(0.05)

    pipeline_service._release_processing_slot("a")
    thread_b.join(timeout=2)
    pipeline_service._release_processing_slot("b")
    thread_c.join(timeout=2)
    pipeline_service._release_processing_slot("c")

    assert order == ["b", "c"]


def test_acquire_processing_slot_returns_false_and_dequeues_when_cancelled_while_waiting(monkeypatch):
    _reset_processing_slots(monkeypatch, limit=1)
    assert pipeline_service._acquire_processing_slot("a") is True

    result = {}

    def waiter():
        result["acquired"] = pipeline_service._acquire_processing_slot("b")

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(0.05)
    pipeline_service._cancelled_jobs.add("b")
    with pipeline_service._processing_condition:
        pipeline_service._processing_condition.notify_all()
    thread.join(timeout=2)

    assert result["acquired"] is False
    assert "b" not in pipeline_service._processing_queue
    assert "b" not in pipeline_service._processing_active
    pipeline_service._release_processing_slot("a")


def test_acquire_processing_slot_does_not_strand_the_queue_when_max_concurrent_jobs_fails(monkeypatch):
    # Regression test: song_id used to be appended to the shared
    # _processing_queue before anything here could fail; a transient failure
    # in max_concurrent_jobs() (or _is_cancelled(), or the wait itself) left
    # it stranded at the head of the queue forever, so every future call for
    # every OTHER song looped forever on `_processing_queue[0] != song_id`
    # since nothing else would ever remove the stranded entry.
    _reset_processing_slots(monkeypatch, limit=1)
    monkeypatch.setattr(pipeline_service.ai_bridge, "max_concurrent_jobs", Mock(side_effect=RuntimeError("boom")))
    raises(RuntimeError, lambda: pipeline_service._acquire_processing_slot("a"), match="boom")
    assert "a" not in pipeline_service._processing_queue

    monkeypatch.setattr(pipeline_service.ai_bridge, "max_concurrent_jobs", Mock(return_value=1))
    assert pipeline_service._acquire_processing_slot("b") is True
    pipeline_service._release_processing_slot("b")


def test_load_job_paths_handles_missing_default_and_persisted_output(monkeypatch, tmp_path):
    database, lookup = mock_song_lookup(monkeypatch, pipeline_service)
    assert pipeline_service._load_job_paths("missing") is None
    database.close.assert_called_once_with()

    current = SimpleNamespace(source_path="source.wav", output_dir=None, slug="song")
    lookup.return_value = current
    monkeypatch.setattr(pipeline_service.config, "SONG_OUTPUT_DIR", tmp_path)
    assert pipeline_service._load_job_paths("song") == ("source.wav", tmp_path / "song")
    current.output_dir = str(tmp_path / "output")
    patch_attrs(monkeypatch, pipeline_service.song_service, resolve_output_dir=Mock(return_value=tmp_path / 'resolved'))
    assert pipeline_service._load_job_paths("song")[1] == tmp_path / "resolved"


def test_load_ai_inputs_respects_trusted_nonempty_text_and_edit_flags(monkeypatch, tmp_path):
    database, lookup = mock_song_lookup(monkeypatch, pipeline_service)
    assert pipeline_service._load_ai_inputs("missing", tmp_path) == (None, None, None)
    current = SimpleNamespace(
        tempo_override=120,
        key_override=" C ",
        tempo_user_edited=True,
        key_user_edited=True,
    )
    lookup.return_value = current
    trusted = tmp_path / pipeline_service.config.TRUSTED_LYRICS_FILENAME
    trusted.write_text("lyrics", encoding="utf-8")
    assert pipeline_service._load_ai_inputs("song", tmp_path) == (trusted, 120.0, "C")
    trusted.write_text(" ", encoding="utf-8")
    current.tempo_user_edited = False
    current.key_user_edited = False
    assert pipeline_service._load_ai_inputs("song", tmp_path) == (None, None, None)


def test_load_searchable_title_prefers_artist_title_and_closes(monkeypatch, tmp_path):
    database, lookup = mock_song_lookup(monkeypatch, pipeline_service)
    assert pipeline_service._load_searchable_title("missing") is None
    current = SimpleNamespace(title="Title", artist="Artist")
    lookup.return_value = current
    assert pipeline_service._load_searchable_title("song") == "Artist - Title"
    current.artist = None
    assert pipeline_service._load_searchable_title("song") == "Title"
    current.title = ""
    assert pipeline_service._load_searchable_title("song") is None


def test_heartbeat_capture_start_stop_and_error_helpers(monkeypatch, tmp_path):
    event, thread = Mock(), Mock()
    patch_attrs(monkeypatch, pipeline_service.threading, Event=Mock(return_value=event), Thread=Mock(return_value=thread))
    assert pipeline_service._start_progress_heartbeat("song") == (event, thread)
    thread.start.assert_called_once_with()

    monkeypatch.setattr(pipeline_service.config, "APP_LOG_DIR", tmp_path)
    capture = pipeline_service._create_progress_capture("song", tmp_path)
    capture.close()
    assert (tmp_path / "application.log").exists()
    pipeline_service._stop_progress_heartbeat(None, thread)
    current = Mock()
    monkeypatch.setattr(pipeline_service.threading, "current_thread", Mock(return_value=current))
    pipeline_service._stop_progress_heartbeat(event, current)
    event.set.assert_called_once_with()
    pipeline_service._stop_progress_heartbeat(event, thread)
    thread.join.assert_called_once_with(timeout=2.0)

    assert (pipeline_service._format_processing_error(ValueError('bad')) == 'ValueError: bad') and (pipeline_service._format_processing_error(RuntimeError()) == 'RuntimeError')
    logger = Mock()
    monkeypatch.setattr(pipeline_service, "logger", logger)
    pipeline_service._write_pipeline_error("song", None, RuntimeError("bad"))
    logger.error.assert_called_once()
    logger.reset_mock()
    writer = Mock()
    pipeline_service._write_pipeline_error("song", writer, RuntimeError("bad"))
    logger.error.assert_called_once()
    writer.write.assert_called_once()
    writer.write.side_effect = OSError("disk")
    pipeline_service._write_pipeline_error("song", writer, RuntimeError("bad"))


def test_ai_progress_callback_updates_semantic_runtime_and_cancels(monkeypatch):
    monkeypatch.setattr(pipeline_service, "_progress_runtime", {})
    pipeline_service._begin_runtime_progress("song")
    patch_many(monkeypatch, (pipeline_service, "_is_cancelled", Mock(return_value=False)), (pipeline_service.time, "monotonic", Mock(side_effect=[10, 20])))
    update, capture = Mock(), Mock()
    monkeypatch.setattr(pipeline_service, "_update_progress", update)
    callback = pipeline_service._create_ai_progress_callback("song", capture)
    callback("decode", -10, "raw")
    callback("unknown", 200, "raw")
    runtime = pipeline_service._progress_runtime["song"]
    assert (runtime['direct_percent'], runtime['completed_stage_seconds']['decode'], capture.write.call_count) == (99.7, 10, 2)
    pipeline_service._is_cancelled.return_value = True
    raises(pipeline_service.ProcessingCancelled, lambda: callback('pitch', 50, 'raw'))


def test_clear_generated_results_preserves_verified_lyrics_and_their_metadata(monkeypatch, tmp_path):
    cache = Mock()
    monkeypatch.setattr(pipeline_service, "StageCache", Mock(return_value=cache))
    (tmp_path / "old.json").write_text("{}", encoding="utf-8")
    (tmp_path / "old.mid").write_bytes(b"midi")
    (tmp_path / "lyricsSync.json").write_text("{}", encoding="utf-8")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    pipeline_service._clear_generated_results(tmp_path)
    cache.invalidate.assert_called_once_with("pitch", "derivation")
    assert {path.name for path in tmp_path.iterdir()} == {
        "lyricsSync.json",
        "metadata.json",
    }


def test_reprocessing_validates_owned_direct_child_and_runs_job(monkeypatch, tmp_path):
    database, current = Mock(), SimpleNamespace()
    monkeypatch.setattr(pipeline_service, "SessionLocal", Mock(return_value=database))
    lookup = Mock(return_value=None)
    monkeypatch.setattr(pipeline_service.repositories, "get_song", lookup)
    pipeline_service._run_reprocessing("missing")
    database.close.assert_called()

    lookup.return_value = current
    root = tmp_path / "library"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(pipeline_service.config, "SONG_OUTPUT_DIR", root)
    monkeypatch.setattr(pipeline_service.config, "SONG_LIBRARY_ROOTS", {root})
    resolve = Mock(return_value=outside)
    monkeypatch.setattr(pipeline_service.song_service, "resolve_output_dir", resolve)
    update = Mock()
    monkeypatch.setattr(pipeline_service, "_update_progress", update)
    pipeline_service._run_reprocessing("song")
    assert update.call_args.kwargs["status"] == models.SongStatus.ERROR

    target = root / "song"
    target.mkdir()
    resolve.return_value = target
    rebuild, run = Mock(), Mock()
    patch_attrs(monkeypatch, pipeline_service, _clear_generated_results=rebuild, _run_job=run)
    pipeline_service._run_reprocessing("song")
    rebuild.assert_called_once_with(target)
    run.assert_called_once_with("song", reuse_vocals=True)

    # A song whose output_dir lives under a HISTORICAL library root (the user
    # changed the storage location after this song was created) must still be
    # reprocessable — resolve_output_dir already trusts it for reads, so the
    # ownership check here must not narrow that back down to only the current root.
    historical_root = tmp_path / "historical-library"
    historical_root.mkdir()
    monkeypatch.setattr(pipeline_service.config, "SONG_LIBRARY_ROOTS", {root, historical_root})
    historical_target = historical_root / "song"
    historical_target.mkdir()
    resolve.return_value = historical_target
    rebuild.reset_mock()
    run.reset_mock()
    pipeline_service._run_reprocessing("song")
    rebuild.assert_called_once_with(historical_target)
    run.assert_called_once_with("song", reuse_vocals=True)


def test_optional_json_and_generated_metadata_preserve_user_edits(monkeypatch, tmp_path):
    assert pipeline_service._read_optional_generated_json(tmp_path / "missing", {"d": 1}) == {
        "d": 1
    }
    patch_attrs(monkeypatch, pipeline_service, read_json=Mock(side_effect=ValueError('bad json')))
    assert pipeline_service._read_optional_generated_json(tmp_path / "bad", []) == []

    write_json(tmp_path / "lyricsSync.json", {"key": "C", "bpm": 120.4, "words": [{"notes": [{"note": 50}, {"note": 70}]}]})
    patch_attrs(monkeypatch, pipeline_service, read_json=__import__('app.utils.json_files', fromlist=['read_json']).read_json)
    current = SimpleNamespace(
        key_override=None,
        tempo_override=None,
        key_user_edited=False,
        tempo_user_edited=False,
        note_range_min=None,
        note_range_max=None,
    )
    pipeline_service._apply_generated_metadata(current, tmp_path)
    assert ((current.key_override, current.tempo_override), (current.note_range_min, current.note_range_max)) == (('C', 120), (50, 70))

    current.key_override = "D"
    current.tempo_override = 100
    current.key_user_edited = True
    current.tempo_user_edited = True
    current.note_range_min = 40
    current.note_range_max = 80
    pipeline_service._apply_generated_metadata(current, tmp_path)
    assert (current.key_override, current.tempo_override, current.note_range_min) == ("D", 100, 40)

    write_json(tmp_path / "lyricsSync.json", {"key": "C", "bpm": 120, "words": [{"notes": [{"note": "bad"}]}]})
    pipeline_service._apply_generated_metadata(current, tmp_path)
    write_json(tmp_path / "lyricsSync.json", {"key": "C", "bpm": 120, "words": [{"notes": []}]})
    pipeline_service._apply_generated_metadata(current, tmp_path)
    write_json(tmp_path / "lyricsSync.json", {"key": "C", "bpm": 120, "words": "bad"})
    pipeline_service._apply_generated_metadata(current, tmp_path)


def test_finalize_success_commits_metadata_and_best_effort_optimization(monkeypatch, tmp_path):
    write_audio(tmp_path / "instrumental.flac")
    write_audio(tmp_path / "vocals.flac")
    current = SimpleNamespace()
    database, lookup = mock_song_lookup(monkeypatch, pipeline_service, current)
    metadata, generated, commit, optimize = Mock(), Mock(), Mock(), Mock(side_effect=RuntimeError('optional failure'))
    patch_attrs(monkeypatch, pipeline_service, _apply_source_metadata=metadata, _persist_confirmed_identity=Mock(), _apply_generated_metadata=generated, commit=commit)
    monkeypatch.setattr(pipeline_service.cache_service, "optimize_song_files", optimize)
    pipeline_service._finalize_success("song", tmp_path)
    assert current.status == models.SongStatus.DONE and current.progress_percent == 100
    commit.assert_called_once_with(database)
    database.close.assert_called_once()
    lookup.return_value = None
    pipeline_service._finalize_success("missing", tmp_path)


def test_audio_pipeline_receives_exact_artist_and_title_as_separate_fields(monkeypatch, tmp_path):
    process = Mock(return_value="result")
    monkeypatch.setattr(pipeline_service.ai_bridge, "process_song", process)
    monkeypatch.setattr(pipeline_service, "_is_cancelled", Mock(return_value=False))
    monkeypatch.setattr(
        pipeline_service, "_create_ai_progress_callback", Mock(return_value=Mock())
    )

    result = pipeline_service._invoke_ai_pipeline(
        "song",
        "source.flac",
        tmp_path,
        None,
        "Exact Artist",
        "Exact Title",
        None,
        None,
        None,
        "fast",
        Mock(),
        reuse_vocals=False,
    )

    assert result == "result"
    assert process.call_args.kwargs["artist"] == "Exact Artist"
    assert process.call_args.kwargs["title"] == "Exact Title"


def test_audio_media_finishes_without_clip_when_search_has_no_valid_result(
    monkeypatch, tmp_path
):
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    write_json(tmp_path / "metadata.json", {"duration": 180, "files": ["cover.jpg"]})
    prepare = Mock(return_value={
        "cover_status": "ready",
        "video_status": "fallback",
        "video_id": None,
        "warnings": ["clip missing"],
    })
    monkeypatch.setattr(
        pipeline_service.metadata_enrichment_service,
        "prepare_training_media",
        prepare,
    )
    monkeypatch.setattr(
        pipeline_service.metadata_enrichment_service,
        "video_file_is_ready",
        Mock(return_value=False),
    )

    pipeline_service._complete_audio_media(
        "song", tmp_path, artist="Artist", title="Title"
    )

    assert not (tmp_path / "clip.mp4").is_file()
    result = pipeline_service.read_json(tmp_path / "metadata.json")
    assert result["media"] == {
        "cover_status": "ready",
        "video_status": "fallback",
        "video_id": None,
    }
    assert "clip.mp4" not in result["files"]


def test_retry_after_clip_failure_reuses_finished_ai_artifacts(monkeypatch, tmp_path):
    """Retrying a media-only failure must not separate and align the song again."""
    events = []
    media_process = SimpleNamespace(
        result=Mock(return_value={"cover_status": "ready", "video_status": "ready"}),
        close=Mock(),
    )
    patch_attrs(
        monkeypatch,
        pipeline_service,
        _load_job_paths=Mock(return_value=("source.flac", tmp_path)),
        _load_song_identity=Mock(return_value=("Artist", "Title")),
        _load_song_genre=Mock(return_value=None),
        _load_ai_inputs=Mock(return_value=(None, None, None)),
        _is_cancelled=Mock(return_value=False),
        _update_progress=Mock(),
        _begin_runtime_progress=Mock(),
        _start_progress_heartbeat=Mock(return_value=(Mock(), Mock())),
        _stop_progress_heartbeat=Mock(),
        _end_runtime_progress=Mock(),
        _acquire_processing_slot=Mock(return_value=True),
        _release_processing_slot=Mock(),
        _create_progress_capture=Mock(return_value=Mock()),
        _audio_artifacts_waiting_for_media=Mock(return_value=True),
        _complete_audio_media=Mock(side_effect=lambda *_a, **_k: events.append("media")),
        _finalize_processed_job=Mock(side_effect=lambda *_a, **_k: events.append("finalize")),
        _log_processing_started=Mock(),
        _log_processing_finished=Mock(),
    )
    monkeypatch.setattr(
        pipeline_service.metadata_enrichment_service,
        "start_training_media_process",
        Mock(return_value=media_process),
    )
    ai = Mock(side_effect=lambda *_a, **_k: events.append("ai"))
    monkeypatch.setattr(pipeline_service, "_invoke_ai_pipeline", ai)

    pipeline_service._run_job("song")

    assert events == ["media", "finalize"]
    ai.assert_not_called()


def test_run_job_orchestrates_success_cancel_error_and_finalization(monkeypatch, tmp_path, caplog):
    events = []
    patch_attrs(monkeypatch, pipeline_service, _load_job_paths=Mock(return_value=('source', tmp_path)), _load_song_identity=Mock(return_value=('Artist', 'Title')), _load_song_genre=Mock(return_value=None), _load_ai_inputs=Mock(return_value=(None, None, None)), _is_cancelled=Mock(return_value=False), _update_progress=Mock(), _begin_runtime_progress=Mock(), _start_progress_heartbeat=Mock(return_value=(Mock(), Mock())), _acquire_processing_slot=Mock(return_value=True), _release_processing_slot=Mock(side_effect=lambda _song_id: events.append("release")), _complete_audio_media=Mock())
    capture = Mock()
    patch_many(monkeypatch, (pipeline_service, "_create_progress_capture", Mock(return_value=capture)), (pipeline_service.model_install_service, "ensure_ready_sync", Mock()))
    patch_attrs(monkeypatch, pipeline_service, _configure_ai_runtime=Mock(return_value='cpu'), format_runtime_plan=Mock(return_value=('CPU',)), _create_ai_progress_callback=Mock(return_value=Mock()))
    process = Mock()
    monkeypatch.setattr(pipeline_service.ai_bridge, "process_song", process)
    media_process = SimpleNamespace(
        result=Mock(return_value={"cover_status": "ready", "video_status": "ready"}),
        close=Mock(),
    )
    start_media = Mock(
        side_effect=lambda *_args, **_kwargs: (
            events.append("start-media"),
            media_process,
        )[1]
    )
    monkeypatch.setattr(
        pipeline_service.metadata_enrichment_service,
        "start_training_media_process",
        start_media,
    )
    pipeline_service.model_install_service.ensure_ready_sync.side_effect = (
        lambda **_kwargs: events.append("models-ready")
    )
    process.side_effect = lambda *_args, **_kwargs: events.append("ai") or "result"
    patch_attrs(monkeypatch, pipeline_service, _stop_progress_heartbeat=Mock(), _end_runtime_progress=Mock())
    finalize = Mock(side_effect=lambda *_args, **_kwargs: events.append("finalize"))
    monkeypatch.setattr(pipeline_service, "_finalize_success", finalize)
    with caplog.at_level("INFO", logger=pipeline_service.logger.name):
        pipeline_service._run_job("song")
    process.assert_called_once()
    messages = [record.getMessage() for record in caplog.records]
    assert any("Song processing started: song_id=song" in message for message in messages)
    assert any("Song processing finished: song_id=song" in message for message in messages)
    finalize.assert_called_once_with("song", tmp_path, retain_source=True)
    assert events.index("start-media") < events.index("models-ready") < events.index("ai")
    assert start_media.call_args.kwargs["expected_duration"] is None
    assert events[-2:] == ["finalize", "release"]
    capture.close.assert_called_once()

    pipeline_service._load_job_paths.return_value = None
    pipeline_service._run_job("missing")
    pipeline_service._load_job_paths.return_value = ("source", tmp_path)
    pipeline_service._is_cancelled.return_value = True
    pipeline_service._run_job("cancel-before")

    pipeline_service._is_cancelled.return_value = False
    process.side_effect = pipeline_service.ProcessingCancelled()
    pipeline_service._run_job("cancel")
    assert (
        pipeline_service._update_progress.call_args.kwargs["status"] == models.SongStatus.CANCELLED
    )

    process.side_effect = RuntimeError("AI failed")
    writer = Mock()
    monkeypatch.setattr(pipeline_service, "_write_pipeline_error", writer)
    pipeline_service._run_job("error")
    assert pipeline_service._update_progress.call_args.kwargs["status"] == models.SongStatus.ERROR
    writer.assert_called()

    pipeline_service._is_cancelled.side_effect = [False, True]
    pipeline_service._run_job("cancel-error")
    assert (
        pipeline_service._update_progress.call_args.kwargs["status"] == models.SongStatus.CANCELLED
    )


def test_run_job_reaches_finalization_even_when_cleanup_itself_raises(monkeypatch, tmp_path, caplog):
    # Regression test: an exception from the cleanup step itself (closing
    # capture, unlinking lyrics_path, stopping the heartbeat, closing
    # media_process) used to propagate straight out of _run_job's finally
    # block, replacing the outcome the try/except above already decided and
    # skipping _finalize_processed_job entirely below -- the song's DB row
    # was silently stranded at PROCESSING forever with no error_message.
    events = []
    patch_attrs(monkeypatch, pipeline_service, _load_job_paths=Mock(return_value=('source', tmp_path)), _load_song_identity=Mock(return_value=('Artist', 'Title')), _load_song_genre=Mock(return_value=None), _load_ai_inputs=Mock(return_value=(None, None, None)), _is_cancelled=Mock(return_value=False), _update_progress=Mock(), _begin_runtime_progress=Mock(), _start_progress_heartbeat=Mock(return_value=(Mock(), Mock())), _acquire_processing_slot=Mock(return_value=True), _release_processing_slot=Mock(side_effect=lambda _song_id: events.append("release")), _complete_audio_media=Mock())
    capture = Mock()
    capture.close.side_effect = OSError("disk full")
    patch_many(monkeypatch, (pipeline_service, "_create_progress_capture", Mock(return_value=capture)), (pipeline_service.model_install_service, "ensure_ready_sync", Mock()))
    patch_attrs(monkeypatch, pipeline_service, _configure_ai_runtime=Mock(return_value='cpu'), format_runtime_plan=Mock(return_value=('CPU',)), _create_ai_progress_callback=Mock(return_value=Mock()))
    process = Mock(side_effect=lambda *_args, **_kwargs: events.append("ai") or "result")
    monkeypatch.setattr(pipeline_service.ai_bridge, "process_song", process)
    monkeypatch.setattr(
        pipeline_service.metadata_enrichment_service, "start_training_media_process", Mock(return_value=None),
    )
    patch_attrs(monkeypatch, pipeline_service, _stop_progress_heartbeat=Mock(), _end_runtime_progress=Mock())
    finalize = Mock(side_effect=lambda *_args, **_kwargs: events.append("finalize"))
    monkeypatch.setattr(pipeline_service, "_finalize_success", finalize)

    with caplog.at_level("ERROR", logger=pipeline_service.logger.name):
        pipeline_service._run_job("song")  # must not raise

    process.assert_called_once()
    finalize.assert_called_once_with("song", tmp_path, retain_source=True)
    # The slot releases twice: once immediately because cleanup_succeeded is
    # False (the pre-existing behavior for a failed cleanup), and again
    # after finalization -- what matters for this regression is that
    # "finalize" is reached at all instead of the exception escaping first.
    assert events == ["ai", "release", "finalize", "release"]
    assert any("cleanup failed" in record.getMessage() for record in caplog.records)


def test_run_job_refuses_full_process_when_source_was_retired_to_instrumental(monkeypatch, tmp_path):
    patch_attrs(
        monkeypatch, pipeline_service,
        _load_job_paths=Mock(return_value=('instrumental.flac', tmp_path)),
        _is_cancelled=Mock(return_value=False),
        _source_unavailable_for_full_process=Mock(return_value=True),
        _update_progress=Mock(),
    )
    process = Mock()
    monkeypatch.setattr(pipeline_service.ai_bridge, "process_song", process)
    pipeline_service._run_job("song")
    process.assert_not_called()
    assert pipeline_service._update_progress.call_args.kwargs["status"] == models.SongStatus.ERROR

    # reprocess (reuse_vocals=True) never reads source_path, so the guard must not apply to it
    reprocess = Mock()
    monkeypatch.setattr(pipeline_service.ai_bridge, "reprocess_song", reprocess)
    patch_attrs(
        monkeypatch, pipeline_service,
        _load_searchable_title=Mock(return_value=None),
        _load_ai_inputs=Mock(return_value=(None, None, None)),
        _begin_runtime_progress=Mock(), _start_progress_heartbeat=Mock(return_value=(None, None)),
        _create_progress_capture=Mock(return_value=Mock()), _acquire_processing_slot=Mock(return_value=True),
        _configure_ai_runtime=Mock(return_value='cpu'), format_runtime_plan=Mock(return_value=('CPU',)),
        _create_ai_progress_callback=Mock(return_value=Mock()),
        _stop_progress_heartbeat=Mock(), _end_runtime_progress=Mock(), _finalize_success=Mock(),
    )
    monkeypatch.setattr(pipeline_service.model_install_service, "ensure_ready_sync", Mock())
    pipeline_service._run_job("song", reuse_vocals=True)
    reprocess.assert_called_once()


def test_run_job_maps_finalization_failure(monkeypatch, tmp_path):
    patch_attrs(monkeypatch, pipeline_service, _load_job_paths=Mock(return_value=('source', tmp_path)), _load_searchable_title=Mock(return_value=None), _load_ai_inputs=Mock(return_value=(None, None, None)), _is_cancelled=Mock(return_value=False), _update_progress=Mock(), _begin_runtime_progress=Mock(), _start_progress_heartbeat=Mock(return_value=(None, None)))
    capture = Mock()
    patch_many(monkeypatch, (pipeline_service, "_create_progress_capture", Mock(return_value=capture)), (pipeline_service.model_install_service, "ensure_ready_sync", Mock()))
    patch_attrs(monkeypatch, pipeline_service, _configure_ai_runtime=Mock(return_value='cpu'), format_runtime_plan=Mock(return_value=('CPU',)), _create_ai_progress_callback=Mock(return_value=Mock()))
    monkeypatch.setattr(pipeline_service.ai_bridge, "process_song", Mock())
    patch_attrs(monkeypatch, pipeline_service, _stop_progress_heartbeat=Mock(), _end_runtime_progress=Mock(), _finalize_success=Mock(side_effect=RuntimeError('commit failed')))
    pipeline_service._run_job("song")
    assert pipeline_service._update_progress.call_args.kwargs["status"] == models.SongStatus.ERROR


def test_validate_artifact_audio_accepts_real_audio_and_rejects_corrupt_or_empty(tmp_path):
    real = tmp_path / "real.flac"
    write_audio(real)
    pipeline_service._validate_artifact_audio(real)  # does not raise

    corrupt = tmp_path / "corrupt.flac"
    corrupt.write_bytes(b"not actually audio")
    raises(ValueError, lambda: pipeline_service._validate_artifact_audio(corrupt), match="not a valid audio file")

    too_short = tmp_path / "too-short.flac"
    write_audio(too_short, seconds=0.001)
    raises(
        ValueError,
        lambda: pipeline_service._validate_artifact_audio(too_short),
        match="no usable audio duration",
    )


def test_finalize_success_rejects_a_corrupt_instrumental_before_marking_the_song_done(monkeypatch, tmp_path):
    current = SimpleNamespace()
    mock_song_lookup(monkeypatch, pipeline_service, current)
    (tmp_path / "instrumental.flac").write_bytes(b"not audio")
    write_audio(tmp_path / "vocals.flac")

    raises(ValueError, lambda: pipeline_service._finalize_success("song", tmp_path), match="not a valid audio file")
    assert not hasattr(current, "status")  # never reached the point of setting a status at all


def test_finalize_success_marks_new_generation_unoptimized_before_best_effort_optimization(monkeypatch, tmp_path):
    song, database = SimpleNamespace(id='song', title='Confirmed title', artist='Confirmed artist', output_dir=str(tmp_path), source_path=str(tmp_path / 'source.wav'), optimized=True, status=models.SongStatus.PROCESSING, progress_percent=50.0, progress_step='processing', error_message=None, key_override=None, tempo_override=None, note_range_min=None, note_range_max=None), Mock()
    (tmp_path / "source.wav").write_bytes(b"source")
    instrumental = tmp_path / "instrumental.flac"
    write_audio(instrumental)
    write_audio(tmp_path / "vocals.flac")
    write_json(tmp_path / "lyricsSync.json", {"bpm": 120, "key": "C", "words": []})
    patch_many(monkeypatch, (pipeline_service, "SessionLocal", Mock(return_value=database)), (pipeline_service.repositories, "get_song", Mock(return_value=song)))
    monkeypatch.setattr(pipeline_service.song_service, "resolve_source_path", lambda current: Path(current.source_path))
    patch_attrs(monkeypatch, pipeline_service, _apply_source_metadata=Mock(), _apply_generated_metadata=Mock())
    commit = Mock()
    patch_many(monkeypatch, (pipeline_service, "commit", commit), (pipeline_service.revision_cache, "invalidate", Mock()))

    pipeline_service._finalize_success("song", tmp_path)

    assert (song.status == models.SongStatus.DONE) and (song.optimized is False)
    assert Path(song.source_path) == instrumental.resolve() and not (tmp_path / "source.wav").exists()
    identity = pipeline_service.read_json(tmp_path / "lyricsSync.json")
    assert (identity["title"], identity["artist"]) == ("Confirmed title", "Confirmed artist")
    commit.assert_called_once_with(database)
