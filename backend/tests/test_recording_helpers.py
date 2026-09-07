import contextlib
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import models
from app.services import recording_service
from tests._shared import make_song, patch_attrs, patch_many, raises


def recording(path, *, duration=12.5): return models.Recording(id='recording', song_id='song', filename='take.wav', path=str(path), duration_sec=duration, sample_rate=48000)


def test_shared_capture_uses_the_host_native_packet_instead_of_the_asio_monitor_buffer(monkeypatch):
    devices = [{"hostapi": 2, "max_input_channels": 2}]
    patch_attrs(
        monkeypatch,
        recording_service.sd,
        query_devices=Mock(return_value=devices[0]),
        query_hostapis=Mock(return_value={"name": "Windows WASAPI"}),
    )

    assert recording_service._capture_blocksize(0, 64) == 0


def test_portaudio_asio_capture_keeps_the_buffer_selected_in_settings(monkeypatch):
    patch_attrs(
        monkeypatch,
        recording_service.sd,
        query_devices=Mock(return_value={"hostapi": 4, "max_input_channels": 2}),
        query_hostapis=Mock(return_value={"name": "ASIO"}),
    )

    assert recording_service._capture_blocksize(7, 64) == 64


def test_capture_rejects_negative_buffer_without_probing_other_devices(monkeypatch):
    patch_attrs(monkeypatch, recording_service.sd, query_devices=Mock(side_effect=RuntimeError('device unavailable')))
    with pytest.raises(RuntimeError, match="non-negative"):
        recording_service._capture_attempts(None, None, 44_100, -1, False)
    recording_service.sd.query_devices.assert_not_called()


def test_plain_recording_keeps_selected_buffer_without_opening_output():
    attempts = recording_service._capture_attempts(1, 2, 44_100, 64, False)
    assert attempts == [(1, None, 44_100, 64, False, 64 / 44100)]
    assert all(not monitor and output is None for _, output, _, _, monitor, _ in attempts)


def test_native_capture_buffer_uses_low_latency_without_forcing_a_tiny_callback():
    assert recording_service._capture_attempts(1, 2, 44_100, 0, False) == [
        (1, None, 44_100, 0, False, "low")
    ]


def test_monitoring_never_raises_selected_block_or_retries_without_monitoring():
    attempts = recording_service._capture_attempts(1, 2, 16_000, 64, True)
    standard_rate_attempts = recording_service._capture_attempts(1, 2, 48_000, 32, True)

    assert attempts == [(1, 2, 16_000, 64, True, 64 / 16000)]
    assert standard_rate_attempts == [(1, 2, 48_000, 32, True, 32 / 48000)]


def test_backend_status_and_session_controls(monkeypatch):
    patch_attrs(monkeypatch, recording_service, _AUDIO_BACKEND_AVAILABLE=False, _AUDIO_BACKEND_ERROR='PortAudio missing')
    assert recording_service.backend_available() == (False, "PortAudio missing")

    session = Mock()
    monkeypatch.setattr(recording_service, "_sessions", {"active": session})
    recording_service.pause_recording("active")
    recording_service.resume_recording("active")
    session.pause.assert_called_once_with()
    session.resume.assert_called_once_with()
    raises(KeyError, lambda: recording_service.pause_recording('missing'), match='missing')

    recording_service.close_all_sessions()
    session.close.assert_called_once_with()
    assert recording_service._sessions == {}


def test_active_recording_controls_replace_all_values_used_by_the_final_mix(monkeypatch):
    session = SimpleNamespace(music_gain=1.0, gain=1.0, effects={})
    monkeypatch.setattr(recording_service, "_sessions", {"active": session})

    recording_service.update_recording_controls(
        "active",
        music_gain=0.25,
        gain=1.75,
        effects={"reverb": 0.4, "echo": 0.3, "delay": 0.2, "octave": -0.5},
    )

    assert session.music_gain == 0.25
    assert session.gain == 1.75
    assert session.effects == {"reverb": 0.4, "echo": 0.3, "delay": 0.2, "octave": -0.5}


def test_close_sessions_for_song_keeps_unrelated_recordings(monkeypatch):
    selected = SimpleNamespace(song_id="song", close=Mock(), stop_capture=Mock())
    other = SimpleNamespace(song_id="other", close=Mock(), stop_capture=Mock())
    monkeypatch.setattr(recording_service, "_sessions", {"selected": selected, "other": other})

    recording_service.close_sessions_for_song("song")

    selected.close.assert_called_once_with()
    selected.stop_capture.assert_called_once_with()
    other.stop_capture.assert_not_called()
    other.close.assert_not_called()
    assert recording_service._sessions == {"other": other}


def test_stop_recording_wakes_the_duration_limit_watcher_before_the_cap(monkeypatch):
    # A recording stopped by the user before it ever reaches its duration
    # cap must not leave _finalize_on_duration_limit's daemon thread blocked
    # on session.limit_reached forever -- one leaked thread per recording,
    # for the rest of the app's lifetime, since nothing else ever set it.
    session = SimpleNamespace(
        song_id="song", limit_reached=threading.Event(), stop_capture=Mock(), close=Mock()
    )
    patch_attrs(
        monkeypatch, recording_service,
        _sessions={"id": session}, _finalizing_recordings={}, _completed_recordings={},
    )
    watcher = threading.Thread(
        target=recording_service._finalize_on_duration_limit, args=("id", session), daemon=True
    )
    watcher.start()
    assert watcher.is_alive()

    # stop_recording does real DB/ffmpeg work past the session pop, which
    # this test has no need to set up -- only the pop + wake matters here.
    monkeypatch.setattr(
        recording_service, "SessionLocal", Mock(side_effect=RuntimeError("stop early for this test"))
    )
    with contextlib.suppress(Exception):
        recording_service.stop_recording("id")

    watcher.join(timeout=2)
    assert not watcher.is_alive()
    session.stop_capture.assert_called_once_with()


def test_start_recording_does_not_hide_driver_failure_with_fallback(monkeypatch):
    patch_many(monkeypatch, (recording_service, "_AUDIO_BACKEND_AVAILABLE", True), (recording_service.uuid, "uuid4", lambda: SimpleNamespace(hex="session")))

    failed = Mock()
    failed.start.side_effect = RuntimeError("WDM host error")
    fallback = Mock()
    factory = Mock(side_effect=[failed, fallback])
    patch_attrs(monkeypatch, recording_service, RecordingSession=factory, _sessions={})

    with pytest.raises(RuntimeError, match="WDM host error"):
        recording_service.start_recording("song", device_id=1, output_device_id=2, sample_rate=48000, blocksize=64, monitoring_enabled=True)
    failed.close.assert_called_once_with()
    factory.assert_called_once()
    fallback.start.assert_not_called()
    assert recording_service._sessions == {}


def test_start_recording_reports_backend_and_final_driver_errors(monkeypatch):
    patch_attrs(monkeypatch, recording_service, _AUDIO_BACKEND_AVAILABLE=False, _AUDIO_BACKEND_ERROR='missing')
    raises(RuntimeError, lambda: recording_service.start_recording('song'), match='missing')

    patch_attrs(monkeypatch, recording_service, _AUDIO_BACKEND_AVAILABLE=True, _capture_attempts=lambda *_args: [(None, None, 44100, 0, False, 'high')], RecordingSession=Mock(side_effect=RuntimeError('driver rejected stream')))
    raises(RuntimeError, lambda: recording_service.start_recording('song'), match='driver rejected stream')


def test_recording_paths_are_confined_to_library(monkeypatch, tmp_path):
    library = tmp_path / "library"
    voice = library / "song" / "recordings" / "take.wav"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"voice")
    monkeypatch.setattr(recording_service.config, "SONG_OUTPUT_DIR", library)
    current = recording(voice)

    assert (recording_service.resolve_recording_path(current) == voice.resolve()) and (recording_service.performance_mix_path(current).name == 'take-performance.mp3') and ([path.name for path in recording_service.performance_mix_paths(current)] == ['take-performance.mp3', 'take-performance.wav'])

    outside = recording(tmp_path / "outside.wav")
    raises(ValueError, lambda: recording_service.resolve_recording_path(outside), match='outside')


def test_recording_deletion_includes_existing_voice_and_mixes(monkeypatch, tmp_path):
    library = tmp_path / "library"
    voice, mix = library / 'take.wav', library / 'take-performance.mp3'
    library.mkdir()
    voice.write_bytes(b"voice")
    mix.write_bytes(b"mix")
    monkeypatch.setattr(recording_service.config, "SONG_OUTPUT_DIR", library)
    delete = Mock()
    monkeypatch.setattr(recording_service, "delete_with_files", delete)
    database, current = Mock(), recording(voice)

    recording_service.delete_recording(database, current)

    assert delete.call_args.args == (database, current, (voice.resolve(), mix.resolve()))


@pytest.mark.parametrize(
    ("name", "amount", "fragment"),
    [
        ("reverb", 0.5, "55|110|170"),
        ("echo", 0.5, "180|360"),
        ("delay", 0.5, "305"),
        ("unknown", 1, None),
        ("echo", 0, None),
    ],
)
def test_effect_filters_are_bounded(name, amount, fragment):
    result = recording_service._effect_filter(name, amount, "in", "out")
    assert (fragment in result) if fragment else result is None


def test_octave_filter_preserves_duration_for_both_directions():
    octave_up = recording_service._effect_filter("octave", 1, "in", "out")
    octave_down = recording_service._effect_filter("octave", -1, "in", "out")

    assert "asetrate=96000.000" in octave_up and "atempo=0.500000" in octave_up
    assert "asetrate=24000.000" in octave_down and "atempo=2.000000" in octave_down


def test_performance_mix_command_contains_raw_timing_and_lossy_output(tmp_path):
    current = recording(tmp_path / "take.wav")
    command = recording_service._performance_mix_command(
        "ffmpeg",
        current,
        tmp_path / "instrumental.mp3",
        tmp_path / "mix.mp3",
        1.25,
        0.8,
        {"reverb": 0.5, "unknown": 1},
    )
    assert (command[command.index('-ss') + 1] == '1.250') and (command[command.index('-t') + 1] == '12.500')
    filters = command[command.index("-filter_complex") + 1]
    assert ('volume=0.800000' in filters) and ('volume=1.650000' in filters)
    # reverb is applied on a separate split-off copy of the voice ("wet"),
    # mixed in alongside the untouched dry [performer-final] branch below --
    # not chained into [performer-final] itself, so that branch's own timing
    # stays exactly as it was (see the comment at its call site). "unknown"
    # is silently ignored, matching _effect_filter's contract.
    assert "aecho" in filters
    dry_chain = next(part for part in filters.split(";") if part.endswith("[performer-final]"))
    assert "aecho" not in dry_chain
    assert "amix=inputs=3" in filters
    assert command[-4:] == ['libmp3lame', '-b:a', '320k', str(tmp_path / 'mix.mp3')]

    wav_command = recording_service._performance_mix_command(
        "ffmpeg", current, tmp_path / "instrumental.mp3", tmp_path / "mix.wav",
        0, 1, {},
    )
    assert wav_command[-3:] == ["-c:a", "pcm_s24le", str(tmp_path / "mix.wav")]

    synchronized = recording_service._performance_mix_command(
        "ffmpeg", current, tmp_path / "instrumental.mp3", tmp_path / "sync.wav",
        0, 0.8, {}, [
            {"start_recording_sec": 0.125, "start_playback_sec": 4.5, "end_recording_sec": 5.0},
            {"start_recording_sec": 5.25, "start_playback_sec": 12.0, "end_recording_sec": 8.0},
        ],
    )
    synchronized_filters = synchronized[synchronized.index("-filter_complex") + 1]
    assert "-ss" not in synchronized
    assert "atrim=start=4.500000:duration=4.875000" in synchronized_filters
    assert "adelay=125:all=1" in synchronized_filters
    assert "amix=inputs=2:duration=longest" in synchronized_filters
    # adelay must run before atempo, not after -- see the comment at its call
    # site: this exact ffmpeg build corrupts the filter graph's PTS
    # bookkeeping (and silently truncates the muxed output to a few KB) when
    # a non-zero adelay follows atempo on a chain that feeds amix.
    music0_chain = next(part for part in synchronized_filters.split(";") if "[music0]" in part)
    assert music0_chain.index("adelay=") < music0_chain.index("atempo=")

    compensated = recording_service._performance_mix_command(
        "ffmpeg", current, tmp_path / "instrumental.mp3", tmp_path / "latency.wav",
        0, 1, {}, [{
            "start_recording_sec": 0.01,
            "start_playback_sec": -0.04,
            "end_recording_sec": 2.0,
        }],
    )
    compensated_filters = compensated[compensated.index("-filter_complex") + 1]
    assert "atrim=start=0.000000" in compensated_filters
    assert "adelay=50:all=1" in compensated_filters


def test_performance_mix_keeps_raw_voice_timing_and_only_applies_slider_gain(tmp_path):
    current = recording(tmp_path / "take.wav")

    command = recording_service._performance_mix_command(
        "ffmpeg",
        current,
        tmp_path / "instrumental.flac",
        tmp_path / "mix.wav",
        0,
        0.7,
        {},
        voice_gain=1.8,
    )

    filters = command[command.index("-filter_complex") + 1]
    voice_chain = next(part for part in filters.split(";") if "[performer-final]" in part)
    assert "volume=2.970000" in voice_chain
    for latency_filter in ("highpass=", "equalizer=", "agate=", "acompressor=", "alimiter=", "aecho=", "asetrate="):
        assert latency_filter not in voice_chain


def test_performance_mix_uses_the_karaoke_playback_rate_for_each_timeline_segment(tmp_path):
    current = recording(tmp_path / "take.wav")
    command = recording_service._performance_mix_command(
        "ffmpeg",
        current,
        tmp_path / "instrumental.flac",
        tmp_path / "mix.wav",
        0,
        1,
        {},
        [{
            "start_recording_sec": 0.25,
            "start_playback_sec": 4.5,
            "end_recording_sec": 5.125,
            "playback_rate": 1.25,
        }],
    )

    filters = command[command.index("-filter_complex") + 1]
    assert "atrim=start=4.500000:duration=6.093750" in filters
    assert "atempo=1.250000" in filters
    # delay_ms = round((0.25 + max(0, -4.5)) * 1000) = 250, pre-scaled by the
    # 1.25 rate (applied before atempo, not after -- see the comment at its
    # call site) so its audible duration after the tempo change still equals
    # the intended 250ms: round(250 * 1.25) = round(312.5) = 312 (Python's
    # banker's rounding rounds a tie to the nearest even integer).
    assert "adelay=312:all=1" in filters
    assert filters.index("adelay=") < filters.index("atempo=")


def test_performance_mix_with_a_delay_produces_the_full_track_via_real_ffmpeg(tmp_path):
    # Regression test for a real ffmpeg bug reproduced against an actual user
    # recording: with adelay AFTER atempo on a chain feeding amix, this
    # ffmpeg build corrupts its own PTS bookkeeping for ANY non-zero delay
    # (independent of track length -- reproduced down to 1-second clips) and
    # silently truncates the muxed output to roughly the delay's own length
    # (a few KB) while still exiting 0 and logging "Application provided
    # invalid, non monotonically increasing dts". None of the existing
    # string-matching tests above ever actually ran ffmpeg, so they could not
    # have caught this -- ffmpeg's own behavior is the only thing that can.
    import shutil
    import subprocess
    from pathlib import Path

    import soundfile as sf

    from tests._shared import write_audio

    ffmpeg = str(recording_service.config.FFMPEG_EXE)
    if not Path(ffmpeg).is_file() and not shutil.which(ffmpeg):
        pytest.skip("real ffmpeg is not available in this environment")

    instrumental = tmp_path / "instrumental.flac"
    write_audio(instrumental, seconds=2.0, sample_rate=44_100, format="FLAC")
    take_path = tmp_path / "take.wav"
    write_audio(take_path, seconds=1.0, sample_rate=44_100, format="WAV")

    current = recording(take_path, duration=1.0)
    destination = tmp_path / "mix.mp3"
    command = recording_service._performance_mix_command(
        ffmpeg, current, instrumental, destination,
        0, 1.0, {}, [{
            "start_recording_sec": 0.2,
            "start_playback_sec": 0.0,
            "end_recording_sec": 1.0,
        }],
    )
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)

    assert result.returncode == 0
    assert "non monotonically" not in result.stderr
    info = sf.info(str(destination))
    assert info.duration > 0.9  # a regression here truncates to ~0.2s or less


def test_instrumental_lookup_and_optional_mix_fail_safely(monkeypatch, tmp_path):
    assert recording_service._find_instrumental(tmp_path) is None
    instrumental = tmp_path / "instrumental.flac"
    instrumental.write_bytes(b"lossless")
    assert recording_service._find_instrumental(tmp_path) == instrumental

    create = Mock(side_effect=RuntimeError("ffmpeg failed"))
    monkeypatch.setattr(recording_service, "_create_performance_mix", create)
    recording_service._create_performance_mix_safely(
        recording(tmp_path / "take.wav"),
        make_song(),
        0,
        1,
    )
    create.assert_called_once()


def test_recording_path_remains_accessible_in_historical_library_root(monkeypatch, tmp_path):
    current, historical = tmp_path / 'current', tmp_path / 'historical'
    current.mkdir()
    voice = historical / "song" / "recordings" / "take.wav"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"voice")
    patch_attrs(monkeypatch, recording_service.config, SONG_OUTPUT_DIR=current, SONG_LIBRARY_ROOTS={current.resolve(), historical.resolve()})

    assert recording_service.resolve_recording_path(recording(voice)) == voice.resolve()
