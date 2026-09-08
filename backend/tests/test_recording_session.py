import builtins
import importlib
import io
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, PropertyMock

import numpy as np
import pytest

import models
from app.services import recording_service
from tests._shared import make_song, mock_song_lookup, patch_attrs, patch_many, raises


def make_session(monkeypatch, **overrides):
    stream = Mock()
    monkeypatch.setattr(recording_service.sd, "InputStream", Mock(return_value=stream))
    options = {
        "session_id": "session",
        "song_id": "song",
        "device_id": 1,
        "output_device_id": 2,
        "sample_rate": 48_000,
        "channels": 1,
        "gain": 1.0,
        "monitoring_enabled": False,
    }
    options.update(overrides)
    return recording_service.RecordingSession(**options), stream


def test_session_initialization_clamps_levels_and_selects_stream(monkeypatch):
    session, stream = make_session(
        monkeypatch,
        gain=8,
        music_gain=-1,
        playback_offset_sec=-2,
        effects={"reverb": 3, "echo": -1, "delay": 0.5},
    )
    assert ((session.gain, session.music_gain, session.playback_offset_sec, session.effects) == (4, 0, 0, {'reverb': 1, 'echo': 0, 'delay': 0.5, 'octave': 0})) and (session._stream is stream)

    duplex = Mock()
    patch_attrs(monkeypatch, recording_service.sd, query_devices=Mock(return_value={'max_output_channels': 2}), Stream=Mock(return_value=duplex))
    monitored, _unused = make_session(monkeypatch, monitoring_enabled=True)
    assert monitored._stream is duplex

    recording_service.sd.query_devices.return_value = {"max_output_channels": 0}
    raises(RuntimeError, lambda: make_session(monkeypatch, monitoring_enabled=True), match='No output')


def test_session_stamps_wav_with_the_sample_rate_reported_by_the_open_driver(monkeypatch):
    """A driver-negotiated rate must win or the saved voice plays at the wrong speed."""
    stream = Mock()
    stream.samplerate = 44_100.0
    monkeypatch.setattr(recording_service.sd, "InputStream", Mock(return_value=stream))

    session = recording_service.RecordingSession(
        session_id="session",
        song_id="song",
        device_id=1,
        output_device_id=2,
        sample_rate=48_000,
        channels=1,
        gain=1.0,
        monitoring_enabled=False,
    )

    assert session.sample_rate == 44_100
    assert session._quality.sample_rate == 44_100
    assert session._effects_chain.sample_rate == 44_100
    assert session._max_frames == 44_100 * recording_service.config.MAX_RECORDING_DURATION_SECONDS


def test_audio_callbacks_save_raw_copies_and_process_only_monitor_output(monkeypatch):
    session, _stream = make_session(monkeypatch, gain=2)
    input_data = np.array([[0.75], [-0.75]], dtype=np.float32)
    session._callback(input_data, 2, None, None)
    recorded = session._queue.get_nowait()
    assert np.array_equal(recorded, input_data)
    assert recorded is not input_data

    output = np.empty((2, 2), dtype=np.float32)
    session._monitoring_enabled = True
    session._monitoring_callback(input_data, output, 2, None, None)
    assert (np.max(np.abs(output)) <= 0.985) and (np.allclose(output[:, 0], output[:, 1]))
    session._monitoring_enabled = False
    output.fill(9)
    session._monitoring_callback(input_data, output, 2, None, None)
    assert not output.any()


def test_dry_monitor_bypass_skips_all_dsp_and_outputs_the_raw_gained_signal(monkeypatch):
    session, _stream = make_session(monkeypatch, gain=2)
    session._monitoring_enabled = True
    session._monitor_dry_bypass = True
    session._quality.process = Mock(side_effect=AssertionError("quality DSP must not run in dry bypass"))
    input_data = np.array([[0.25], [-0.25]], dtype=np.float32)
    output = np.empty((2, 2), dtype=np.float32)

    session._monitoring_callback(input_data, output, 2, None, None)

    assert session._capture_error is None
    assert np.allclose(output[:, 0], [0.5, -0.5])
    assert np.allclose(output[:, 1], output[:, 0])


def test_recording_keeps_raw_microphone_frames_when_monitor_dsp_changes_length(monkeypatch):
    """Monitor DSP must never shorten, resample, or otherwise rewrite the saved take."""
    patch_attrs(
        monkeypatch,
        recording_service.sd,
        query_devices=Mock(return_value={"max_output_channels": 2}),
        Stream=Mock(return_value=Mock()),
    )
    session, _stream = make_session(monkeypatch, monitoring_enabled=True)
    input_data = np.arange(8, dtype=np.float32).reshape(8, 1) / 16
    # Reproduce a processor/driver path that returns fewer frames than arrived.
    session._quality.process = Mock(return_value=np.full((3, 1), 0.25, dtype=np.float32))
    output = np.empty((8, 2), dtype=np.float32)

    session._monitoring_callback(input_data, output, 8, None, None)

    recorded = session._queue.get_nowait()
    assert recorded.shape == input_data.shape
    assert np.array_equal(recorded, input_data)
    assert session._timeline_frames == len(input_data)


def test_callback_stops_enqueueing_once_writer_has_died(monkeypatch):
    # TASK 4.1: once the writer thread has crashed (disk full, I/O error, ...)
    # nothing drains the queue anymore — the audio callback must stop feeding
    # it, or RAM grows unbounded for as long as the session stays open.
    session, _stream = make_session(monkeypatch)
    input_data = np.zeros((2, 1), dtype=np.float32)
    session._callback(input_data, 2, None, None)
    assert session._queue.qsize() == 1

    session._writer_error = RuntimeError("disk full")
    session._callback(input_data, 2, None, None)
    assert session._queue.qsize() == 1  # unchanged: the crashed-writer frame was dropped


def test_audio_callbacks_expose_dsp_failures_and_silence_monitor_output(monkeypatch):
    patch_attrs(
        monkeypatch,
        recording_service.sd,
        query_devices=Mock(return_value={"max_output_channels": 2}),
        Stream=Mock(return_value=Mock()),
    )
    session, _stream = make_session(monkeypatch, monitoring_enabled=True)
    failure = RuntimeError("DSP failed")
    session._quality.process = Mock(side_effect=failure)
    input_data = np.zeros((4, 1), dtype=np.float32)

    # An input-only recording no longer invokes monitor DSP at all.
    session._callback(input_data, 4, None, None)
    assert session._capture_error is None
    assert np.array_equal(session._queue.get_nowait(), input_data)

    output = np.full((4, 2), 1.0, dtype=np.float32)
    session._monitoring_callback(input_data, output, 4, None, None)
    # A self-monitoring DSP failure must never discard the raw take -- only
    # _capture_error does that (see stop_and_save).
    assert session._capture_error is None
    assert session._monitor_error is failure
    assert not output.any()


def test_stop_writer_marks_overflow_for_the_block_it_drops_to_make_room(monkeypatch):
    # _stop_writer() drops one real, never-written chunk of audio to make
    # room for its terminal marker when the queue is already full -- that
    # used to happen silently, so the take was reported as complete and
    # correct despite actually being missing whatever that block held
    # (often the very last moment of the take).
    session, _stream = make_session(monkeypatch)
    session._writer_thread = Mock(is_alive=Mock(return_value=True))
    session._queue = recording_service.queue.Queue(maxsize=1)
    dropped_chunk = np.zeros((7, 1), dtype=np.float32)
    session._queue.put_nowait(dropped_chunk)

    session._stop_writer()

    assert session.overflow_stats == {"dropped_blocks": 1, "dropped_frames": 7}
    assert isinstance(session._overflow_error, recording_service.RecordingOverflowError)
    assert session._queue.get_nowait() is recording_service.RecordingSession._WRITER_STOP


def test_queue_overflow_is_bounded_and_marks_the_recording_incomplete(monkeypatch, caplog):
    # TASK 4.1: a real-time audio callback must never block on a full queue —
    # dropping the frame is the only safe option, and the queue itself must
    # have a finite capacity so a slow/stalled writer can't grow RAM forever.
    session, _stream = make_session(monkeypatch)
    assert session._queue.maxsize > 0
    for _ in range(session._queue.maxsize):
        session._queue.put_nowait(object())
    assert session._queue.full()

    input_data = np.zeros((2, 1), dtype=np.float32)
    with caplog.at_level("ERROR"):
        session._callback(input_data, 2, None, None)  # must not raise/block even though the queue is full
    assert session._queue.qsize() == session._queue.maxsize
    assert session.overflow_stats == {"dropped_blocks": 1, "dropped_frames": 2}
    assert isinstance(session._overflow_error, recording_service.RecordingOverflowError)
    assert any("Recording queue overflow" in record.getMessage() for record in caplog.records)

    # Once integrity is lost, later callback blocks are counted as dropped and
    # are never appended behind the already stalled writer.
    session._callback(input_data, 2, None, None)
    assert session.overflow_stats == {"dropped_blocks": 2, "dropped_frames": 4}


def test_queue_overflow_never_publishes_a_successful_recording(monkeypatch, tmp_path, caplog):
    session, _stream = make_session(monkeypatch)
    temporary = tmp_path / "partial.wav"
    temporary.write_bytes(b"incomplete")
    destination = tmp_path / "published.wav"
    session._temporary_path = temporary
    session._queue = recording_service.queue.Queue(maxsize=1)
    session._queue.put_nowait(object())

    session._callback(np.zeros((64, 1), dtype=np.float32), 64, None, None)

    with caplog.at_level("ERROR"), pytest.raises(
        recording_service.RecordingOverflowError,
        match="Recording is incomplete",
    ):
        session.stop_and_save(destination)

    assert not destination.exists()
    assert not temporary.exists()
    assert any(
        "Discarding incomplete recording" in record.getMessage()
        and "dropped_frames=64" in record.getMessage()
        for record in caplog.records
    )


def test_playback_segments_follow_captured_frames_and_pause_boundaries(monkeypatch):
    session, _stream = make_session(monkeypatch, sample_rate=100)
    session._callback(np.zeros((10, 1), dtype=np.float32), 10, None, None)
    session.sync_playback(2.5)
    session._callback(np.zeros((25, 1), dtype=np.float32), 25, None, None)
    session.pause()

    assert session.playback_segments == [{
        "start_recording_sec": pytest.approx(0.1),
        "start_playback_sec": 2.5,
        "end_recording_sec": pytest.approx(0.35),
    }]
    raises(RuntimeError, lambda: session.sync_playback(3), match="paused")

    session.resume()
    session.sync_playback(8)
    session._callback(np.zeros((20, 1), dtype=np.float32), 20, None, None)
    session.pause()
    assert session.playback_segments[1] == {
        "start_recording_sec": pytest.approx(0.35),
        "start_playback_sec": 8,
        "end_recording_sec": pytest.approx(0.55),
    }


def test_playback_anchor_includes_audio_waiting_in_the_device_buffer(monkeypatch):
    session, stream = make_session(monkeypatch, sample_rate=100)
    stream.time = 10.15
    timing = SimpleNamespace(inputBufferAdcTime=10.0)
    session._callback(np.zeros((10, 1), dtype=np.float32), 10, timing, None)

    session.sync_playback(4.0)

    assert session.playback_segments[0]["start_recording_sec"] == pytest.approx(0.15)


def test_playback_anchor_keeps_the_reported_media_position_without_latency_adjustment(monkeypatch):
    session, _stream = make_session(
        monkeypatch,
        sample_rate=100,
        playback_offset_sec=0,
    )
    session._callback(np.zeros((10, 1), dtype=np.float32), 10, None, None)

    session.sync_playback(0.02)

    assert session.playback_segments[0]["start_playback_sec"] == pytest.approx(0.02)


def test_playback_anchor_tracks_rate_without_moving_the_reported_media_position(monkeypatch):
    session, _stream = make_session(
        monkeypatch,
        sample_rate=100,
        playback_offset_sec=0,
    )
    session._callback(np.zeros((10, 1), dtype=np.float32), 10, None, None)

    session.sync_playback(2.0, 1.25)

    assert session.playback_segments[0]["start_playback_sec"] == pytest.approx(2.0)
    assert session.playback_segments[0]["playback_rate"] == 1.25


def test_timeline_uses_captured_frames_when_driver_clock_is_unavailable(monkeypatch):
    session, stream = make_session(monkeypatch)
    session._timeline_frames = 4800
    session._last_capture_end_clock = 10.1
    clock = PropertyMock(side_effect=recording_service.sd.PortAudioError("Error getting stream time"))
    monkeypatch.setattr(type(stream), "time", clock, raising=False)
    assert session._timeline_time() == pytest.approx(.1)


@pytest.mark.parametrize("capture_already_stopped", [False, True])
def test_save_active_segment_never_reads_closed_stream_clock(monkeypatch, tmp_path, capture_already_stopped):
    session, stream = make_session(monkeypatch)
    session._temporary_path = tmp_path / "temporary.wav"
    recording_service.sf.write(session._temporary_path, np.zeros((9600, 1)), session.sample_rate)
    session._frames_written = session._timeline_frames = 9600
    session._last_capture_end_clock = 10.2
    # Even a live clock must not extend the final segment past the saved WAV.
    clock = PropertyMock(return_value=10.25)
    monkeypatch.setattr(type(stream), "time", clock, raising=False)
    session._active_playback_segment = {"start_recording_sec": 0.0, "start_playback_sec": 1.0}
    session._playback_segments.append(session._active_playback_segment)
    def close_stream():
        clock.side_effect = recording_service.sd.PortAudioError("Error getting stream time")
    stream.close.side_effect = close_stream
    if capture_already_stopped:
        session.stop_capture()
    destination = tmp_path / "saved.wav"
    duration, rate = session.stop_and_save(destination)
    assert duration == pytest.approx(.2)
    assert session.playback_segments[0]["end_recording_sec"] == pytest.approx(duration)
    assert recording_service.sf.info(destination).frames == 9600
    clock.assert_not_called()
    stream.stop.assert_called_once()
    stream.close.assert_called_once()


def test_writer_persists_chunks_and_reports_library_errors(monkeypatch, tmp_path, caplog):
    session, _stream = make_session(monkeypatch)
    session._temporary_path = tmp_path / "take.wav"
    output = MagicMock()
    output.__enter__.return_value = output
    monkeypatch.setattr(recording_service.sf, "SoundFile", Mock(return_value=output))
    session._queue.put(np.zeros((3, 1)))
    session._queue.put(session._WRITER_STOP)
    session._write_audio()
    output.write.assert_called_once()
    assert (session._frames_written == 3) and (session._writer_ready.is_set())

    failed, _stream = make_session(monkeypatch)
    failed._temporary_path = tmp_path / "failed.wav"
    patch_attrs(monkeypatch, recording_service.sf, SoundFile=Mock(side_effect=RuntimeError('disk full')))
    with caplog.at_level("ERROR"):
        failed._write_audio()
    assert (str(failed._writer_error) == 'disk full') and (failed._writer_ready.is_set())
    assert any(
        failed.session_id in record.getMessage() and "disk full" in record.getMessage()
        for record in caplog.records
    )


def test_write_audio_stops_and_signals_once_duration_limit_is_reached(monkeypatch, tmp_path):
    monkeypatch.setattr(recording_service.config, "MAX_RECORDING_DURATION_SECONDS", 1)
    session, _stream = make_session(monkeypatch, sample_rate=2)  # max_frames == 2
    session._temporary_path = tmp_path / "take.wav"
    output = MagicMock()
    output.__enter__.return_value = output
    monkeypatch.setattr(recording_service.sf, "SoundFile", Mock(return_value=output))
    session._queue.put(np.zeros((3, 1)))  # already exceeds the 2-frame limit
    session._queue.put(np.zeros((3, 1)))  # must never be consumed

    session._write_audio()

    assert session.limit_reached.is_set() and session._frames_written == 3
    output.write.assert_called_once()


def test_finalize_on_duration_limit_ignores_test_doubles(monkeypatch):
    stop = Mock()
    monkeypatch.setattr(recording_service, "stop_recording", stop)
    recording_service._finalize_on_duration_limit("id", Mock())  # Mock().limit_reached isn't a real Event
    stop.assert_not_called()


def test_finalize_on_duration_limit_stops_a_still_active_session(monkeypatch):
    session, _stream = make_session(monkeypatch)
    session.limit_reached.set()
    monkeypatch.setattr(recording_service, "_sessions", {"id": session})
    stop = Mock()
    monkeypatch.setattr(recording_service, "stop_recording", stop)

    recording_service._finalize_on_duration_limit("id", session)

    stop.assert_called_once_with("id")


def test_finalize_on_duration_limit_skips_a_session_the_user_already_stopped(monkeypatch):
    session, _stream = make_session(monkeypatch)
    session.limit_reached.set()
    monkeypatch.setattr(recording_service, "_sessions", {})
    stop = Mock()
    monkeypatch.setattr(recording_service, "stop_recording", stop)

    recording_service._finalize_on_duration_limit("id", session)

    stop.assert_not_called()


def test_start_writer_closes_descriptor_and_cleans_failed_file(monkeypatch, tmp_path):
    session, _stream = make_session(monkeypatch)
    temporary = tmp_path / "temporary.wav"
    temporary.write_bytes(b"partial")
    patch_attrs(monkeypatch, recording_service.tempfile, mkstemp=Mock(return_value=(7, str(temporary))))
    close = Mock()
    monkeypatch.setattr(recording_service.os, "close", close)

    class ImmediateThread:
        def __init__(self, *, target, **_options):
            self.target = target

        def start(self): session._writer_ready.set()

    monkeypatch.setattr(recording_service.threading, "Thread", ImmediateThread)
    session._start_writer()
    close.assert_called_once_with(7)
    assert session._temporary_path == temporary

    failed, _stream = make_session(monkeypatch)
    temporary.write_bytes(b"partial")
    failed._writer_error = RuntimeError("cannot open")

    class FailedThread(ImmediateThread):
        def start(self): failed._writer_ready.set()

    monkeypatch.setattr(recording_service.threading, "Thread", FailedThread)
    raises(RuntimeError, lambda: failed._start_writer(), match='cannot open')
    assert not temporary.exists()


def test_cleanup_temporary_file_tolerates_a_still_open_handle(monkeypatch, tmp_path):
    # missing_ok=True only suppresses "file not found", not Windows holding
    # the handle open (WinError 32) because a timed-out writer thread never
    # actually exited. This runs from close(), itself called from a bare
    # `finally:` in stop_recording() -- letting it propagate would replace
    # whatever response was already in flight and skip the cleanup right
    # after it, just because a small leaked temp file couldn't be deleted.
    session, _stream = make_session(monkeypatch)
    session._temporary_path = tmp_path / "still-open.wav"

    def locked_unlink(self, missing_ok=False):
        raise PermissionError(32, "The process cannot access the file because it is being used by another process")

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    session._cleanup_temporary_file()  # must not raise
    assert session._temporary_path is None


def test_session_lifecycle_cleans_resources_on_errors(monkeypatch, tmp_path):
    session, stream = make_session(monkeypatch)
    start_writer, stop_writer, cleanup = Mock(), Mock(), Mock()
    patch_attrs(monkeypatch, session, _start_writer=start_writer, _stop_writer=stop_writer, _cleanup_temporary_file=cleanup)
    stream.start.side_effect = RuntimeError("host error")
    raises(RuntimeError, lambda: session.start(), match='host error')
    stop_writer.assert_called_once_with()
    cleanup.assert_called_once_with()

    stream.start.side_effect = None
    stream.start.reset_mock()
    session.pause()
    assert session._paused is True
    stream.stop.assert_not_called()
    session.resume()
    assert session._paused is False
    stream.stop.assert_not_called()
    stream.start.assert_not_called()

    session.close()
    session.close()
    stream.close.assert_called_once_with()

    idle, _stream = make_session(monkeypatch)
    idle._stop_writer()
    temporary = tmp_path / "cleanup.wav"
    temporary.write_bytes(b"x")
    idle._temporary_path = temporary
    idle._cleanup_temporary_file()
    assert idle._temporary_path is None and not temporary.exists()


def test_stop_writer_signals_live_thread(monkeypatch):
    session, _stream = make_session(monkeypatch)
    thread = Mock()
    thread.is_alive.return_value = True
    session._writer_thread = thread
    session._stop_writer()
    assert session._queue.get_nowait() is session._WRITER_STOP
    thread.join.assert_called_once_with(timeout=5.0)
    assert session._writer_thread is None


def test_stop_and_save_publishes_atomic_recording(monkeypatch, tmp_path):
    session, stream = make_session(monkeypatch, sample_rate=4)
    temporary, destination = tmp_path / 'temporary.wav', tmp_path / 'library' / 'take.wav'
    temporary.write_bytes(b"audio")
    session._temporary_path = temporary
    session._frames_written = 8
    session._writer_thread = Mock(is_alive=Mock(return_value=False))

    duration, sample_rate = session.stop_and_save(destination)

    assert ((duration, sample_rate) == (2.0, 4)) and (destination.read_bytes() == b'audio') and (session._temporary_path is None)
    stream.stop.assert_called_once_with()
    stream.close.assert_called_once_with()

    raises(RuntimeError, lambda: session.stop_and_save(destination), match='already closed')


def test_stop_and_save_survives_a_locked_temp_file_after_a_successful_publish(monkeypatch, tmp_path, caplog):
    # The recording is already successfully published to out_path by the
    # time the redundant temp copy is cleaned up -- a transient "file still
    # open" OSError deleting it (Windows, e.g. an AV/indexer scan) must not
    # propagate and make stop_recording() delete the just-published
    # recording and report failure for a take that actually succeeded.
    session, _stream = make_session(monkeypatch, sample_rate=4)
    temporary, destination = tmp_path / 'temporary.wav', tmp_path / 'library' / 'take.wav'
    temporary.write_bytes(b"audio")
    session._temporary_path = temporary
    session._frames_written = 8
    session._writer_thread = Mock(is_alive=Mock(return_value=False))

    real_unlink = Path.unlink

    def locked_unlink(self, missing_ok=False):
        if self == temporary:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", locked_unlink)

    with caplog.at_level("WARNING"):
        duration, sample_rate = session.stop_and_save(destination)

    assert (duration, sample_rate) == (2.0, 4)
    assert destination.read_bytes() == b"audio"
    assert session._temporary_path is None
    assert temporary.exists()  # could not actually be deleted, but that's harmless


def test_stop_and_save_reports_writer_failure(monkeypatch, tmp_path, caplog):
    session, _stream = make_session(monkeypatch)
    temporary = tmp_path / "temporary.wav"
    temporary.write_bytes(b"audio")
    session._temporary_path = temporary
    session._writer_error = RuntimeError("disk full")

    with caplog.at_level("ERROR"):
        raises(RuntimeError, lambda: session.stop_and_save(tmp_path / 'take.wav'), match='write')
    assert not temporary.exists()


def test_stop_and_save_still_succeeds_when_only_the_teardown_fails(monkeypatch, tmp_path, caplog):
    # Every sample is already captured and enqueued before the stream is
    # even asked to stop (the callback simply stops firing once it is) --
    # the device raising while closing must not discard an already-complete,
    # valid take. Logged as a warning instead of discarding it.
    session, stream = make_session(monkeypatch)
    temporary = tmp_path / "temporary.wav"
    temporary.write_bytes(b"audio")
    session._temporary_path = temporary
    stream.stop.side_effect = RuntimeError("device lost")

    with caplog.at_level("WARNING"):
        duration_sec, sample_rate = session.stop_and_save(tmp_path / "take.wav")

    assert (tmp_path / "take.wav").exists() and not temporary.exists()
    assert sample_rate == session.sample_rate and duration_sec >= 0
    assert any("device lost" in record.getMessage() for record in caplog.records)


def test_stop_and_save_requires_initialized_file(monkeypatch, tmp_path):
    session, _stream = make_session(monkeypatch)
    raises(RuntimeError, lambda: session.stop_and_save(tmp_path / 'take.wav'), match='not initialized')


def test_stop_recording_calls_on_capture_released_before_the_performance_mix(monkeypatch, tmp_path):
    # The input/output device is already fully released by
    # session.stop_capture() at the very top of stop_recording() -- the
    # caller's on_capture_released hook (the router restoring monitoring)
    # must fire there, not only after the WAV write and ffmpeg performance
    # mix further below have also finished.
    session = Mock(
        song_id="song", playback_offset_sec=0.0, music_gain=1.0, gain=1.0,
        effects={}, playback_segments=[],
    )
    session.stop_and_save.return_value = (1.0, 48_000)
    monkeypatch.setattr(recording_service, "_sessions", {"session": session})
    current_song = make_song()
    database, _ = mock_song_lookup(monkeypatch, recording_service, current_song)
    patch_attrs(monkeypatch, recording_service.song_service, resolve_output_dir=Mock(return_value=tmp_path / 'song'))
    order = []
    commit = Mock(side_effect=lambda _db, item: item)
    mix = Mock(side_effect=lambda *_args, **_kwargs: order.append("mix"))
    patch_attrs(monkeypatch, recording_service, commit_refresh=commit, _create_performance_mix_safely=mix)

    recording_service.stop_recording(
        "session", on_capture_released=lambda: order.append("released")
    )

    assert order == ["released", "mix"]
    session.stop_capture.assert_called_once_with()


def test_stop_recording_persists_take_and_always_closes_resources(monkeypatch, tmp_path):
    segments = [{
        "start_recording_sec": 0.25,
        "start_playback_sec": 1.5,
        "end_recording_sec": 3.0,
    }]
    session = Mock(
        song_id="song",
        playback_offset_sec=1.5,
        music_gain=0.8,
        gain=1.4,
        effects={"reverb": 0.2},
        playback_segments=segments,
    )
    session.stop_and_save.return_value = (3.0, 48_000)
    monkeypatch.setattr(recording_service, "_sessions", {"session": session})
    current_song = make_song()
    database, _ = mock_song_lookup(monkeypatch, recording_service, current_song)
    patch_attrs(monkeypatch, recording_service.song_service, resolve_output_dir=Mock(return_value=tmp_path / 'song'))
    commit, mix = Mock(side_effect=lambda _db, item: item), Mock()
    patch_attrs(monkeypatch, recording_service, commit_refresh=commit, _create_performance_mix_safely=mix)

    result = recording_service.stop_recording("session")

    assert result.song_id == "song" and result.duration_sec == 3 and result.playback_offset_sec == 1.25
    assert result.playback_segments_json == (
        '[{"start_recording_sec":0.25,"start_playback_sec":1.5,"end_recording_sec":3.0}]'
    )
    database.add.assert_called_once_with(result)
    mix.assert_called_once_with(
        result, current_song, 1.25, 0.8, {"reverb": 0.2}, segments, 1.4, 1.0
    )
    session.close.assert_called_once_with()
    database.close.assert_called_once_with()


def test_stop_recording_keeps_song_readable_after_the_session_closes(monkeypatch, tmp_path):
    # Regression test: commit_refresh's db.commit() expires every object
    # still attached to the session (SQLAlchemy's expire_on_commit default),
    # not just the `recording` row it explicitly refreshes -- `song` is a
    # separate object fetched earlier in the same session. stop_recording
    # closes that session before calling _create_performance_mix_safely(...,
    # song, ...); without an explicit db.refresh(song) first, the first
    # attribute access on song there raises DetachedInstanceError (silently
    # swallowed, so the performance mix is just never produced). A fully
    # mocked `db` (see the sibling test above) can't catch this -- it never
    # exercises real commit/expire/close semantics.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import database

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    database.Base.metadata.create_all(bind=engine)
    RealSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with RealSession() as setup:
        setup.add(make_song(tmp_path))
        setup.commit()
    monkeypatch.setattr(recording_service, "SessionLocal", RealSession)

    session = Mock(song_id="song", playback_offset_sec=0, music_gain=1, gain=1, effects={}, playback_segments=[])
    session.stop_and_save.return_value = (1.0, 48_000)
    monkeypatch.setattr(recording_service, "_sessions", {"session": session})
    patch_attrs(monkeypatch, recording_service.song_service, resolve_output_dir=Mock(return_value=tmp_path))

    accessed = {}

    def capture_mix(_recording, song, *_args):
        accessed["output_dir"] = song.output_dir  # raises DetachedInstanceError if unfixed

    monkeypatch.setattr(recording_service, "_create_performance_mix_safely", capture_mix)

    recording_service.stop_recording("session")

    assert accessed["output_dir"] == str(tmp_path)


def test_stop_recording_does_not_wait_for_the_shared_hardware_lock(monkeypatch, tmp_path):
    # An unrelated monitor-process (re)start can hold hardware_lock for
    # several seconds. stop_recording() must be able to pop the session and
    # stop ITS OWN stream immediately regardless -- it used to also take
    # hardware_lock here, so an unlucky settings change in flight could
    # delay a user's own stop button for that whole time.
    session = Mock(song_id="song", playback_offset_sec=0, music_gain=1, effects={}, playback_segments=[])
    session.stop_and_save.return_value = (1.0, 48_000)
    monkeypatch.setattr(recording_service, "_sessions", {"session": session})
    current_song = make_song()
    mock_song_lookup(monkeypatch, recording_service, current_song)
    patch_attrs(
        monkeypatch,
        recording_service.song_service,
        resolve_output_dir=Mock(return_value=tmp_path / "song"),
    )
    patch_attrs(
        monkeypatch,
        recording_service,
        commit_refresh=Mock(side_effect=lambda _db, item: item),
        _create_performance_mix_safely=Mock(),
    )

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lock():
        with recording_service.hardware_lock:
            lock_acquired.set()
            release_lock.wait(timeout=2)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert lock_acquired.wait(timeout=1)
        started = time.monotonic()
        recording_service.stop_recording("session")
        assert time.monotonic() - started < 0.5
    finally:
        release_lock.set()
        holder.join(timeout=2)


def test_stop_recording_handles_missing_session_song_and_save_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(recording_service, "_sessions", {})
    raises(KeyError, lambda: recording_service.stop_recording('missing'))

    session = Mock(song_id="missing")
    monkeypatch.setattr(recording_service, "_sessions", {"missing-song": session})
    database, _ = mock_song_lookup(monkeypatch, recording_service)
    raises(ValueError, lambda: recording_service.stop_recording('missing-song'))
    session.close.assert_called_once_with()
    database.close.assert_called_once_with()

    current_song, failed = make_song(), Mock(song_id='song')
    failed.stop_and_save.side_effect = RuntimeError("disk full")
    patch_many(monkeypatch, (recording_service, "_sessions", {"failed": failed}), (recording_service.repositories, "get_song", Mock(return_value=current_song)))
    patch_attrs(monkeypatch, recording_service.song_service, resolve_output_dir=Mock(return_value=tmp_path))
    raises(RuntimeError, lambda: recording_service.stop_recording('failed'), match='disk full')
    database.rollback.assert_called()


def test_stop_recording_returns_recently_completed_recording(monkeypatch):
    saved = models.Recording(id="recording", song_id="song", filename="take.wav", path="take.wav", duration_sec=3, sample_rate=48_000)
    database, lookup = Mock(), Mock(return_value=saved)
    patch_many(
        monkeypatch,
        (recording_service, "_sessions", {}),
        (recording_service, "_completed_recordings", {"completed": "recording"}),
        (recording_service, "SessionLocal", Mock(return_value=database)),
        (recording_service.repositories, "get_recording", lookup),
    )

    assert recording_service.stop_recording("completed") is saved
    lookup.assert_called_once_with(database, "recording")
    database.close.assert_called_once_with()


def test_stop_recording_waits_for_concurrent_finalization(monkeypatch):
    saved = models.Recording(id="recording", song_id="song", filename="take.wav", path="take.wav", duration_sec=3, sample_rate=48_000)
    database, event = Mock(), Mock()
    completed = {}

    def finish_first_request(*, timeout):
        assert timeout == 240.0
        completed["active"] = "recording"
        return True

    event.wait.side_effect = finish_first_request
    patch_many(
        monkeypatch,
        (recording_service, "_sessions", {}),
        (recording_service, "_completed_recordings", completed),
        (recording_service, "_finalizing_recordings", {"active": event}),
        (recording_service, "SessionLocal", Mock(return_value=database)),
        (recording_service.repositories, "get_recording", Mock(return_value=saved)),
    )

    assert recording_service.stop_recording("active") is saved
    event.wait.assert_called_once_with(timeout=240.0)


def test_create_performance_mix_runs_ffmpeg_and_cleans_failure(monkeypatch, tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    song_dir = tmp_path / "song"
    song_dir.mkdir()
    instrumental = song_dir / "instrumental.flac"
    instrumental.write_bytes(b"music")
    voice = song_dir / "take.wav"
    voice.write_bytes(b"voice")
    current, current_song = models.Recording(id='recording', song_id='song', filename='take.wav', path=str(voice), duration_sec=4, sample_rate=48000), make_song(output_dir=str(song_dir))
    patch_attrs(monkeypatch, recording_service.config, FFMPEG_EXE=str(executable), SONG_OUTPUT_DIR=tmp_path)
    patch_attrs(monkeypatch, recording_service.song_service, resolve_output_dir=Mock(return_value=song_dir))
    run = Mock()
    monkeypatch.setattr(recording_service.subprocess, "run", run)

    recording_service._create_performance_mix(current, current_song, 0, 1)
    run.assert_called_once()

    destination = song_dir / "take-performance.mp3"
    destination.write_bytes(b"partial")
    run.side_effect = OSError("ffmpeg crashed")
    recording_service._create_performance_mix(current, current_song, 0, 1)
    assert not destination.exists()

    current_song.output_dir = None
    run.reset_mock()
    recording_service._create_performance_mix(current, current_song, 0, 1)
    run.assert_not_called()


def test_create_performance_mix_resolves_path_ffmpeg_and_falls_back_to_wav(monkeypatch, tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    instrumental = tmp_path / "instrumental.flac"
    instrumental.write_bytes(b"music")
    voice = tmp_path / "take.wav"
    voice.write_bytes(b"voice")
    current = models.Recording(
        id="recording", song_id="song", filename="take.wav", path=str(voice),
        duration_sec=4, sample_rate=48000,
    )
    current_song = make_song(output_dir=str(tmp_path))
    patch_many(
        monkeypatch,
        (recording_service.config, "FFMPEG_EXE", "ffmpeg"),
        (recording_service.song_service, "resolve_output_dir", Mock(return_value=tmp_path)),
        (recording_service, "resolve_recording_path", Mock(return_value=voice)),
        (recording_service.shutil, "which", Mock(return_value=str(executable))),
    )
    run = Mock(side_effect=[subprocess.CalledProcessError(1, "ffmpeg"), None])
    monkeypatch.setattr(recording_service.subprocess, "run", run)

    recording_service._create_performance_mix(current, current_song, 0, 0.8, {"reverb": 0.5})

    assert run.call_count == 2
    first, second = (call.args[0] for call in run.call_args_list)
    assert first[0] == str(executable) and first[-1].endswith("-performance.mp3")
    assert second[0] == str(executable) and second[-1].endswith("-performance.wav")
    filters = second[second.index("-filter_complex") + 1]
    assert "volume=1.650000[performer-final]" in filters
    # reverb=0.5 does apply now (see _performance_mix_command), on a
    # separate split-off "wet" copy of the voice, not on the dry
    # [performer-final] branch asserted above -- gate/compressor/limiter are
    # still never applied to the offline mix at all (see the "raw voice
    # timing" test/comment on why).
    assert "aecho" in filters
    dry_chain = next(part for part in filters.split(";") if part.endswith("[performer-final]"))
    assert "aecho" not in dry_chain
    assert "alimiter" not in filters and "acompressor" not in filters


def test_create_performance_mix_skips_missing_instrumental(monkeypatch, tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    current_song = make_song(output_dir=str(tmp_path))
    monkeypatch.setattr(recording_service.config, "FFMPEG_EXE", str(executable))
    patch_attrs(monkeypatch, recording_service.song_service, resolve_output_dir=Mock(return_value=tmp_path))
    run = Mock()
    monkeypatch.setattr(recording_service.subprocess, "run", run)
    recording_service._create_performance_mix(
        models.Recording(
            id="recording",
            song_id="song",
            filename="take.wav",
            path=str(tmp_path / "take.wav"),
        ),
        current_song,
        0,
        1,
    )
    run.assert_not_called()


def test_attach_room_audio_preserves_existing_mix_and_adds_remote_voice(monkeypatch, tmp_path):
    executable = tmp_path / "ffmpeg.exe"
    executable.write_bytes(b"binary")
    voice = tmp_path / "take.wav"
    voice.write_bytes(b"local")
    mix = tmp_path / "take-performance.mp3"
    mix.write_bytes(b"local-effects-and-music")
    current = models.Recording(
        id="recording",
        song_id="song",
        filename="take.wav",
        path=str(voice),
        duration_sec=4,
        sample_rate=48_000,
        playback_offset_sec=0,
        playback_segments_json=(
            '[{"start_recording_sec":0.1,"start_playback_sec":0.2,"end_recording_sec":4}]'
        ),
    )
    current_song = make_song(output_dir=str(tmp_path))
    patch_attrs(
        monkeypatch,
        recording_service.config,
        FFMPEG_EXE=str(executable),
        SONG_OUTPUT_DIR=tmp_path,
    )
    monkeypatch.setattr(recording_service, "resolve_recording_path", Mock(return_value=voice))
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"duet")

    monkeypatch.setattr(recording_service.subprocess, "run", run)
    destination = recording_service.attach_room_audio(
        current, current_song, io.BytesIO(b"remote-opus"), 0.2, 0.05
    )

    assert destination.read_bytes() == b"remote-opus"
    assert mix.read_bytes() == b"duet"
    filter_graph = commands[0][commands[0].index("-filter_complex") + 1]
    assert "[0:a][room]amix=inputs=2" in filter_graph
    assert "adelay=50:all=1" in filter_graph


def test_audio_backend_import_failure_degrades_without_crashing(monkeypatch):
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name in {"sounddevice", "soundfile"}: raise ImportError("optional audio dependency missing")
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", blocked_import)
        reloaded = importlib.reload(recording_service)
        assert reloaded.backend_available() == (False, "optional audio dependency missing")

    importlib.reload(recording_service)


def test_stop_capture_does_not_wait_for_the_shared_hardware_lock(monkeypatch):
    # stop_capture() used to be @serialized (the same RLock a slow ASIO
    # monitor-process (re)start can hold for seconds). An unrelated settings
    # change in flight could then delay stopping THIS session's own stream
    # for that whole time -- during which the real-time queue could overflow
    # and stop_and_save() would discard an otherwise-complete take.
    session, _stream = make_session(monkeypatch)
    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def hold_lock():
        with recording_service.hardware_lock:
            lock_acquired.set()
            release_lock.wait(timeout=2)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    try:
        assert lock_acquired.wait(timeout=1)
        started = time.monotonic()
        session.stop_capture()
        assert time.monotonic() - started < 0.5
    finally:
        release_lock.set()
        holder.join(timeout=2)


def test_stop_capture_is_idempotent_under_concurrent_callers(monkeypatch):
    # Without its own lock, two threads racing stop_capture() (e.g. the
    # duration-limit watchdog and a user-initiated stop) could both pass the
    # "already stopped" check before either flips it, and both close the
    # same underlying stream concurrently.
    session, stream = make_session(monkeypatch)
    barrier = threading.Barrier(4)

    def call_stop_capture():
        barrier.wait(timeout=2)
        session.stop_capture()

    threads = [threading.Thread(target=call_stop_capture) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert stream.close.call_count == 1


def test_close_defers_storage_release_until_a_stuck_writer_thread_actually_exits(monkeypatch):
    # _stop_writer() gives up waiting after its own timeout if the writer is
    # still flushing to a slow/stuck disk. close() used to release the
    # storage-budget reservation immediately regardless, letting a
    # concurrent reservation elsewhere believe that space was free while
    # bytes were still landing on disk.
    session, _stream = make_session(monkeypatch)
    reservation = Mock()
    session._storage_reservations = [reservation]

    release_gate = threading.Event()
    stuck_thread = threading.Thread(target=release_gate.wait, daemon=True)
    stuck_thread.start()
    session._writer_thread = stuck_thread
    monkeypatch.setattr(session, "_stop_writer", Mock())
    monkeypatch.setattr(session, "_cleanup_temporary_file", Mock())

    try:
        session.close()
        reservation.release.assert_not_called()
        assert session._storage_reservations == []
    finally:
        release_gate.set()
        stuck_thread.join(timeout=2)

    for _ in range(50):
        if reservation.release.called:
            break
        time.sleep(0.02)
    reservation.release.assert_called_once_with()


def test_close_serializes_concurrent_callers_instead_of_racing_the_fast_path(monkeypatch):
    # _stop_writer() unconditionally resets self._writer_thread to None once
    # it returns, even when the writer is still actually alive (its own
    # join() timed out). A second, concurrent close() caller (e.g. app
    # shutdown's close_all_sessions() racing an in-flight /recording/stop
    # for the same session) used to take an "already closed" fast path that
    # read that already-None self._writer_thread and release the storage
    # reservation immediately instead of waiting for the still-running
    # writer like the real, in-progress close() call does.
    session, _stream = make_session(monkeypatch)
    reservation = Mock()
    session._storage_reservations = [reservation]
    session._writer_thread = Mock(is_alive=Mock(return_value=True))

    entered = threading.Event()
    release_first = threading.Event()

    def fake_stop_writer():
        entered.set()
        release_first.wait(2)
        # Mirrors the real _stop_writer(): unconditionally nulls this even
        # though the writer_thread close() itself already captured is still
        # "alive" in this test.
        session._writer_thread = None

    monkeypatch.setattr(session, "_stop_writer", fake_stop_writer)
    monkeypatch.setattr(session, "_cleanup_temporary_file", Mock())
    monkeypatch.setattr(session, "stop_capture", Mock())

    first_returned = threading.Event()
    second_returned = threading.Event()

    def first_close():
        session.close()
        first_returned.set()

    def second_close():
        entered.wait(2)
        session.close()
        second_returned.set()

    first = threading.Thread(target=first_close, daemon=True)
    second = threading.Thread(target=second_close, daemon=True)
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.05)
    # The second caller must block behind the first (still in flight)
    # instead of racing past it via the old fast path.
    assert not second_returned.is_set()
    assert reservation.release.call_count == 0

    release_first.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert first_returned.is_set() and second_returned.is_set()
    assert session._storage_reservations == []
    reservation.release.assert_called_once_with()


def test_close_releases_storage_immediately_when_writer_already_stopped(monkeypatch):
    session, _stream = make_session(monkeypatch)
    reservation = Mock()
    session._storage_reservations = [reservation]
    session._writer_thread = None
    monkeypatch.setattr(session, "_stop_writer", Mock())
    monkeypatch.setattr(session, "_cleanup_temporary_file", Mock())

    session.close()
    reservation.release.assert_called_once_with()
    assert session._storage_reservations == []
