from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, call

import numpy as np
import pytest
import soundfile as sf

from AI.audio_metadata_v2 import AudioMetadata, resolve_audio_metadata
from AI.audio_pipeline_v2 import (
    AudioPipelineV2,
    AudioPipelineV2Request,
    build_audio_lyrics_document,
    validate_audio_artifacts,
)
from AI.errors import EngineUnavailableError, ProcessingCancelledError
from AI.lyrics_sources import (
    LyricsDiscovery,
    TimedLine,
)
from AI.models import PitchFrame, VocalNote, Word


def test_audio_document_matches_the_reference_shape_without_internal_fields():
    words = [Word(1.0, 1.8, "Песня", 0.91, 0)]
    notes = [VocalNote(1.0, 1.8, 64, word_index=0)]

    payload = build_audio_lyrics_document(
        artist="Исполнитель",
        title="Название",
        text="Песня",
        bpm=120.0,
        key="Am",
        duration=10.0,
        words=words,
        notes=notes,
    )

    assert list(payload) == [
        "schemaVersion", "bpm", "duration", "key", "reference_audio",
        "text", "words", "source", "title", "artist",
    ]
    assert payload["reference_audio"] == "original.flac"
    assert payload["source"] == "audio"
    assert payload["title"] == "Название"
    assert payload["artist"] == "Исполнитель"
    assert set(payload["words"][0]) == {"text", "start", "end", "notes", "syllables"}


def test_audio_artifact_contract_requires_every_reference_output(tmp_path: Path):
    for name in ("original.flac", "vocals.flac", "instrumental.flac"):
        sf.write(
            tmp_path / name,
            np.zeros((44_100, 2), dtype=np.float32),
            44_100,
            subtype="PCM_24",
        )
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "bpm": 120,
            "duration": 1,
            "key": "C",
            "reference_audio": "original.flac",
            "text": "тест",
            "words": [{
                "text": "тест",
                "start": 0.1,
                "end": 0.2,
                "notes": [{"note": 60, "start": 0.1, "end": 0.2}],
            }],
            "source": "audio",
            "title": "Тест",
            "artist": "Автор",
        }),
        encoding="utf-8",
    )
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="cover"):
        validate_audio_artifacts(tmp_path)

    (tmp_path / "cover.jpg").write_bytes(b"reference-cover")
    validate_audio_artifacts(tmp_path)


def test_audio_artifact_contract_rejects_catastrophically_sparse_melody(tmp_path: Path):
    for name in ("original.flac", "vocals.flac", "instrumental.flac"):
        sf.write(
            tmp_path / name,
            np.zeros((44_100, 2), dtype=np.float32),
            44_100,
            subtype="PCM_24",
        )
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    words = [
        {
            "text": f"word-{index}",
            "start": index * 0.04,
            "end": index * 0.04 + 0.04,
            "notes": (
                [{"note": 60, "start": 0.0, "end": 0.04}]
                if index == 0
                else []
            ),
        }
        for index in range(20)
    ]
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "bpm": 120,
            "duration": 1,
            "key": "C",
            "reference_audio": "original.flac",
            "text": " ".join(word["text"] for word in words),
            "words": words,
            "source": "audio",
            "title": "Тест",
            "artist": "Автор",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="melody notes"):
        validate_audio_artifacts(tmp_path)


def test_audio_artifact_contract_rejects_text_without_synchronized_words(tmp_path: Path):
    for name in ("original.flac", "vocals.flac", "instrumental.flac"):
        sf.write(
            tmp_path / name,
            np.zeros((44_100, 2), dtype=np.float32),
            44_100,
            subtype="PCM_24",
        )
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "bpm": 120,
            "duration": 1,
            "key": "C",
            "reference_audio": "original.flac",
            "text": "unsynchronized lyrics",
            "words": [],
            "source": "audio",
            "title": "Song",
            "artist": "Artist",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="synchronized words"):
        validate_audio_artifacts(tmp_path)


def test_audio_artifact_contract_rejects_json_duration_that_disagrees_with_audio(
    tmp_path: Path,
):
    for name in ("original.flac", "vocals.flac", "instrumental.flac"):
        sf.write(
            tmp_path / name,
            np.zeros((44_100, 2), dtype=np.float32),
            44_100,
            subtype="PCM_24",
        )
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "bpm": 120,
            "duration": 8.4,
            "key": "C",
            "reference_audio": "original.flac",
            "text": "тест",
            "words": [{"text": "тест", "start": 0.1, "end": 0.5, "notes": []}],
            "source": "audio",
            "title": "Тест",
            "artist": "Автор",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duration.*original.flac"):
        validate_audio_artifacts(tmp_path)


def test_audio_artifact_contract_rejects_old_mono_vocals(tmp_path: Path):
    for name in ("original.flac", "instrumental.flac"):
        sf.write(tmp_path / name, [[0.0, 0.0], [0.0, 0.0]], 44_100, subtype="PCM_24")
    sf.write(tmp_path / "vocals.flac", [0.0, 0.0], 44_100, subtype="PCM_24")
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({"bpm": 120, "key": "C", "words": []}), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="stereo"):
        validate_audio_artifacts(tmp_path)


def test_audio_artifact_contract_rejects_overlapping_words(tmp_path: Path):
    for name in ("original.flac", "vocals.flac", "instrumental.flac"):
        sf.write(
            tmp_path / name,
            np.zeros((44_100, 2), dtype=np.float32),
            44_100,
            subtype="PCM_24",
        )
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "metadata.json").write_text("{}", encoding="utf-8")
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "bpm": 120,
            "duration": 1,
            "key": "C",
            "reference_audio": "original.flac",
            "text": "первое второе",
            "words": [
                {"text": "первое", "start": 0.1, "end": 0.5, "notes": []},
                {"text": "второе", "start": 0.4, "end": 0.7, "notes": []},
            ],
            "source": "audio",
            "title": "Тест",
            "artist": "Автор",
        }),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="overlapping words"):
        validate_audio_artifacts(tmp_path)


def test_v2_request_requires_exact_artist_and_title(tmp_path: Path):
    source = tmp_path / "song.flac"
    source.write_bytes(b"audio")

    with pytest.raises(ValueError, match="artist"):
        AudioPipelineV2Request(source, tmp_path / "out", artist="", title="Song")
    with pytest.raises(ValueError, match="title"):
        AudioPipelineV2Request(source, tmp_path / "out", artist="Artist", title="")


def test_new_audio_pipeline_is_a_separate_implementation():
    # Ordinary audio uploads must not silently route back through the legacy
    # KaraokePipeline.run implementation.
    assert AudioPipelineV2.run.__qualname__.startswith("AudioPipelineV2.")


def test_audio_pipeline_reports_every_timed_stage(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.flac"
    sf.write(source, np.zeros((44_100, 2), dtype=np.float32), 44_100, subtype="PCM_24")

    def decode(_source, destination, _sample_rate, channels):
        sf.write(
            destination,
            np.zeros((44_100, channels), dtype=np.float32),
            44_100,
            subtype="PCM_24",
        )

    def separate(_source, vocals, instrumental, **_options):
        stereo = np.zeros((44_100, 2), dtype=np.float32)
        sf.write(vocals, stereo, 44_100, subtype="PCM_24")
        sf.write(instrumental, stereo, 44_100, subtype="PCM_24")

    def cover(_urls, destination):
        Path(destination).write_bytes(b"cover")
        return True

    monkeypatch.setattr("AI.audio_pipeline_v2.decode_audio", decode)
    monkeypatch.setattr("AI.audio_pipeline_v2.duration", lambda _path: 1.0)
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.resolve_audio_metadata",
        lambda **_options: AudioMetadata(
            artist="Artist",
            title="Song",
            genre="Rock",
            cover_url="https://example.test/cover.jpg",
        ),
    )
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.discover_lyrics",
        lambda *_args, **_options: None,
    )
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.analyze_music",
        lambda _path: {"bpm": 120.0, "key": "C"},
    )
    monkeypatch.setattr("AI.audio_pipeline_v2.release_torch_memory", lambda: None)
    monkeypatch.setattr(AudioPipelineV2, "_download_cover_candidates", staticmethod(cover))

    engines = SimpleNamespace(
        separator=SimpleNamespace(name="separator", separate=separate, close=Mock()),
        pitch=SimpleNamespace(name="pitch", estimate=Mock(return_value=[]), close=Mock()),
        transcriber=SimpleNamespace(name="transcriber", close=Mock()),
        aligner=SimpleNamespace(name="aligner", close=Mock()),
    )
    pipeline = AudioPipelineV2(engines=engines)
    words = [Word(0.1, 0.5, "hello", 1.0, 0)]
    notes = [VocalNote(0.1, 0.5, 60, word_index=0)]
    monkeypatch.setattr(
        pipeline,
        "_align",
        lambda *_args, **_options: ("hello", words, "test", []),
    )
    monkeypatch.setattr(
        pipeline,
        "_fit_song_notes",
        lambda *_args, **_options: (words, notes),
    )

    result = pipeline.run(
        AudioPipelineV2Request(
            source,
            tmp_path / "output",
            artist="Artist",
            title="Song",
        )
    )

    assert [report.stage for report in result.reports] == [
        "decode",
        "separate",
        "analysis",
        "align",
        "notes",
        "metadata",
        "validate",
    ]


def test_audio_v2_reprocesses_existing_vocals_without_replacing_stems(tmp_path: Path):
    for name in ("original.flac", "vocals.flac", "instrumental.flac"):
        sf.write(
            tmp_path / name,
            np.zeros((44_100, 2), dtype=np.float32),
            44_100,
            subtype="PCM_24",
        )
    (tmp_path / "cover.jpg").write_bytes(b"cover")
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "bpm": 120,
            "duration": 1,
            "key": "C",
            "reference_audio": "original.flac",
            "text": "hello",
            "words": [{
                "text": "hello",
                "start": 0.1,
                "end": 0.5,
                "notes": [{"note": 60, "start": 0.1, "end": 0.5}],
            }],
            "source": "audio",
            "title": "Song",
            "artist": "Artist",
        }),
        encoding="utf-8",
    )
    (tmp_path / "metadata.json").write_text(
        json.dumps({
            "preparation_mode": "audio-v2",
            "artist": "Artist",
            "title": "Song",
            "word_count": 1,
            "note_count": 1,
        }),
        encoding="utf-8",
    )
    stem_bytes = {
        name: (tmp_path / name).read_bytes()
        for name in ("original.flac", "vocals.flac", "instrumental.flac")
    }
    pitch = [
        PitchFrame(time, 440.0, 0.9, True, 0.5)
        for time in (0.10, 0.12, 0.14, 0.16, 0.18)
    ]
    engines = SimpleNamespace(
        pitch=SimpleNamespace(name="pitch", estimate=Mock(return_value=pitch)),
        aligner=SimpleNamespace(
            name="aligner",
            needs_voice_anchoring=False,
            set_cancelled=Mock(),
            align_long_text=Mock(
                side_effect=AssertionError(
                    "melody reprocessing must preserve existing word alignment"
                )
            ),
        ),
        transcriber=SimpleNamespace(name="transcriber", close=Mock()),
    )

    result = AudioPipelineV2(engines=engines).reprocess_song(
        tmp_path,
        artist="Artist",
        title="Song",
        language="English",
    )

    payload = json.loads((tmp_path / "lyricsSync.json").read_text(encoding="utf-8"))
    assert result.output_dir == tmp_path
    assert payload["reference_audio"] == "original.flac"
    assert payload["words"][0]["notes"]
    engines.aligner.align_long_text.assert_not_called()
    assert {
        name: (tmp_path / name).read_bytes()
        for name in stem_bytes
    } == stem_bytes


def test_online_metadata_fills_missing_genre_and_cover_but_preserves_user_values():
    calls = []

    def provider(artist: str, title: str):
        calls.append((artist, title))
        return AudioMetadata(
            artist=artist,
            title=title,
            genre="Rock",
            cover_url="https://example.test/cover.jpg",
            video_url="https://example.test/clip",
        )

    result = resolve_audio_metadata(
        artist="Artist",
        title="Song",
        genre="Alternative",
        cover_url=None,
        providers=(provider,),
    )

    assert calls == [("Artist", "Song")]
    assert result.genre == "Alternative"
    assert result.cover_url == "https://example.test/cover.jpg"


def test_online_metadata_keeps_verified_cover_fallbacks_from_all_providers():
    def first(artist: str, title: str):
        return AudioMetadata(
            artist=artist,
            title=title,
            cover_url="https://first.test/cover.jpg",
        )

    def second(artist: str, title: str):
        return AudioMetadata(
            artist=artist,
            title=title,
            cover_url="https://second.test/cover.jpg",
        )

    result = resolve_audio_metadata(
        artist="Artist",
        title="Song",
        providers=(first, second),
    )

    assert result.cover_urls == (
        "https://first.test/cover.jpg",
        "https://second.test/cover.jpg",
    )


def test_cover_download_tries_the_next_verified_candidate(tmp_path: Path):
    downloader = Mock(side_effect=[False, True])

    ready = AudioPipelineV2._download_cover_candidates(
        ("https://first.test/cover.jpg", "https://second.test/cover.jpg"),
        tmp_path / "cover.jpg",
        downloader=downloader,
    )

    assert ready is True
    assert downloader.call_args_list == [
        call("https://first.test/cover.jpg", tmp_path / "cover.jpg"),
        call("https://second.test/cover.jpg", tmp_path / "cover.jpg"),
    ]


def test_online_metadata_rejects_a_different_recording():
    def wrong_provider(_artist: str, _title: str):
        return AudioMetadata(
            artist="Other artist",
            title="Other song",
            genre="Rock",
            cover_url="https://example.test/wrong.jpg",
        )

    result = resolve_audio_metadata(
        artist="Artist",
        title="Song",
        providers=(wrong_provider,),
    )

    assert result.artist == "Artist"
    assert result.title == "Song"
    assert result.genre is None
    assert result.cover_url is None


def test_online_metadata_accepts_provider_transliteration():
    def provider(_artist: str, _title: str):
        return AudioMetadata(
            artist="Splean",
            title="Романс",
            genre="Alternative",
            cover_url="https://example.test/romance.jpg",
        )

    result = resolve_audio_metadata(
        artist="Сплин", title="Романс", providers=(provider,)
    )

    assert result.genre == "Alternative"
    assert result.cover_url == "https://example.test/romance.jpg"


def test_complete_online_text_is_forced_aligned_without_ctc_rewriting(tmp_path):
    aligner = SimpleNamespace(
        transcribe_ctc=Mock(side_effect=AssertionError("must not rewrite lyrics")),
        set_cancelled=Mock(),
        align_long_text=Mock(return_value=[
            Word(1.0, 1.4, "Первая", 0.9, 0),
            Word(1.5, 2.0, "строка", 0.9, 1),
        ]),
    )
    pipeline = AudioPipelineV2(engines=SimpleNamespace(aligner=aligner))
    request = AudioPipelineV2Request(
        source_path=tmp_path / "song.flac",
        output_dir=tmp_path,
        artist="Исполнитель",
        title="Песня",
    )

    text, words, source, score_lines = pipeline._align(
        request,
        tmp_path / "vocals.flac",
        LyricsDiscovery(
            "Первая строка",
            "internet",
            "query",
            lines=(TimedLine(1.0, "Первая строка"),),
        ),
    )

    assert text == "Первая строка"
    assert [word.text for word in words] == ["Первая", "строка"]
    assert source == "internet"
    assert len(score_lines) == 1
    assert score_lines[0].text == "Первая строка"
    assert score_lines[0].first_word == 0
    assert score_lines[0].last_word == 1
    aligner.transcribe_ctc.assert_not_called()
    aligner.align_long_text.assert_called_once()


def test_asr_text_without_direct_timestamps_is_forced_aligned(tmp_path):
    transcriber = SimpleNamespace(
        transcribe=Mock(return_value=("Первая вторая", [])),
    )
    aligner = SimpleNamespace(
        set_cancelled=Mock(),
        align_long_text=Mock(return_value=[
            Word(1.0, 1.3, "Первая", 0.9, 0),
            Word(1.4, 1.8, "вторая", 0.9, 1),
        ]),
    )
    pipeline = AudioPipelineV2(
        engines=SimpleNamespace(transcriber=transcriber, aligner=aligner)
    )
    request = AudioPipelineV2Request(
        tmp_path / "song.flac", tmp_path, artist="Исполнитель", title="Песня"
    )

    text, words, source, score_lines = pipeline._align(
        request, tmp_path / "vocals.flac", None
    )

    assert text == "Первая вторая"
    assert [word.text for word in words] == ["Первая", "вторая"]
    assert source == "asr"
    assert len(score_lines) == 1
    transcriber.transcribe.assert_called_once_with(
        tmp_path / "vocals.flac",
        None,
        context="Исполнитель — Песня",
    )
    aligner.align_long_text.assert_called_once_with(
        tmp_path / "vocals.flac", "Первая вторая", None
    )


def test_asr_chunk_boundaries_are_preserved_for_forced_alignment(tmp_path):
    timed_lines = (
        (5.0, "Первая вторая"),
        (15.0, "третья четвертая"),
    )
    transcriber = SimpleNamespace(
        transcribe=Mock(return_value=("Первая вторая\nтретья четвертая", [])),
        last_timed_lines=timed_lines,
    )
    aligned = [
        Word(5.1, 5.5, "Первая", 0.9, 0),
        Word(5.6, 6.0, "вторая", 0.9, 1),
        Word(15.1, 15.5, "третья", 0.9, 2),
        Word(15.6, 16.0, "четвертая", 0.9, 3),
    ]
    aligner = SimpleNamespace(
        set_cancelled=Mock(),
        align_chunked_lines=Mock(return_value=aligned),
        align_timed_lines=Mock(side_effect=AssertionError("ASR chunks are not LRC")),
        align_long_text=Mock(side_effect=AssertionError("chunk timing must not be lost")),
    )
    pipeline = AudioPipelineV2(
        engines=SimpleNamespace(transcriber=transcriber, aligner=aligner)
    )
    request = AudioPipelineV2Request(
        tmp_path / "song.flac", tmp_path, artist="Исполнитель", title="Песня"
    )

    text, words, source, _score_lines = pipeline._align(
        request, tmp_path / "vocals.flac", None
    )

    assert text == "Первая вторая\nтретья четвертая"
    assert [word.text for word in words] == [
        "Первая", "вторая", "третья", "четвертая",
    ]
    assert source == "asr"
    aligner.align_chunked_lines.assert_called_once_with(
        tmp_path / "vocals.flac",
        text,
        (TimedLine(5.0, "Первая вторая"), TimedLine(15.0, "третья четвертая")),
        None,
    )


def test_verified_previous_online_lyrics_are_available_when_lookup_is_down(tmp_path):
    (tmp_path / "lyricsSync.json").write_text(
        json.dumps({
            "schemaVersion": 1,
            "bpm": 120,
            "duration": 10,
            "key": "C",
            "reference_audio": "original.flac",
            "text": "Надёжный полный текст",
            "words": [
                {
                    "text": "Надёжный",
                    "start": 1.0,
                    "end": 1.5,
                    "notes": [],
                }
            ],
            "source": "audio",
            "title": "Песня",
            "artist": "Исполнитель",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "metadata.json").write_text(
        json.dumps({
            "preparation_mode": "audio-v2",
            "title": "Песня",
            "artist": "Исполнитель",
            "lyrics_source": "LRCLIB",
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    cached = AudioPipelineV2._cached_lyrics(
        tmp_path, artist="Исполнитель", title="Песня"
    )

    assert cached is not None
    assert cached.text == "Надёжный полный текст"
    assert cached.source == "cache:LRCLIB"


def test_online_lyrics_failure_falls_back_to_verified_cached_lyrics():
    cached = LyricsDiscovery(
        text="Сохранённый полный текст",
        source="cache:LRCLIB",
        query="Исполнитель - Песня",
    )

    resolved = AudioPipelineV2._resolve_discovered_lyrics(
        None,
        cached,
    )

    assert resolved is cached


def test_fresh_online_lyrics_take_precedence_over_cached_lyrics():
    cached = LyricsDiscovery("Старый текст", "cache:LRCLIB", "old")
    fresh = LyricsDiscovery("Новый текст", "Genius", "new")

    resolved = AudioPipelineV2._resolve_discovered_lyrics(fresh, cached)

    assert resolved is fresh


def test_transient_lyrics_lookup_failure_is_retried_before_asr(monkeypatch):
    recovered = LyricsDiscovery(
        "Полный текст после повторной попытки",
        "LRCLIB",
        "Исполнитель - Песня",
    )
    lookup = Mock(side_effect=[None, recovered])
    monkeypatch.setattr("AI.audio_pipeline_v2.discover_lyrics", lookup)

    result = AudioPipelineV2._discover_lyrics_reliably(
        "Песня",
        "Исполнитель",
    )

    assert result is recovered
    assert lookup.call_count == 2
    lookup.assert_called_with("Песня", "Исполнитель", complete=True)


def test_successful_lyrics_lookup_is_not_repeated(monkeypatch):
    found = LyricsDiscovery("Полный текст", "LRCLIB", "Исполнитель - Песня")
    lookup = Mock(return_value=found)
    monkeypatch.setattr("AI.audio_pipeline_v2.discover_lyrics", lookup)

    result = AudioPipelineV2._discover_lyrics_reliably("Песня", "Исполнитель")

    assert result is found
    lookup.assert_called_once_with("Песня", "Исполнитель", complete=True)


def test_network_lookups_cannot_block_local_music_analysis():
    release = threading.Event()
    started = [threading.Event(), threading.Event()]

    def blocking_task(index: int):
        started[index].set()
        release.wait(timeout=2.0)

    executor = ThreadPoolExecutor(
        max_workers=AudioPipelineV2._parallel_worker_count(),
    )
    try:
        executor.submit(blocking_task, 0)
        executor.submit(blocking_task, 1)
        assert all(event.wait(timeout=1.0) for event in started)

        local_analysis = executor.submit(lambda: "music-ready")

        assert local_analysis.result(timeout=0.5) == "music-ready"
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_waiting_for_background_lookup_honors_processing_cancellation():
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(lambda: release.wait(timeout=2.0))

        with pytest.raises(ProcessingCancelledError):
            AudioPipelineV2._background_result(
                future,
                cancelled=lambda: True,
            )
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_score_lines_remain_valid_when_adjacent_words_share_an_onset():
    words = [
        Word(1.0, 1.08, "Первая", 0.9, 0),
        Word(1.0, 1.10, "Вторая", 0.9, 1),
    ]

    lines = AudioPipelineV2._score_lines(words, ["Первая", "Вторая"])

    assert len(lines) == 2
    assert all(line.end > line.start for line in lines)
    assert lines[0].end >= words[0].end


def test_online_text_is_normalized_before_alignment_and_publication(tmp_path):
    aligner = SimpleNamespace(
        set_cancelled=Mock(),
        align_long_text=Mock(side_effect=lambda _audio, text, _language: [
            Word(index * 0.1, index * 0.1 + 0.05, token, 0.9, index)
            for index, token in enumerate(text.split())
        ]),
    )
    pipeline = AudioPipelineV2(engines=SimpleNamespace(aligner=aligner))
    request = AudioPipelineV2Request(
        tmp_path / "song.flac", tmp_path, artist="Artist", title="Song"
    )

    text, words, _source, _lines = pipeline._align(
        request,
        tmp_path / "vocals.flac",
        LyricsDiscovery(
            "Hа дачу\nОна жуёт оpбит\nА ты жуй-жуй",
            "internet",
            "query",
        ),
    )

    assert text == "На дачу\nОна жуёт орбит\nА ты жуй жуй"
    assert [word.text for word in words][-2:] == ["жуй", "жуй"]
    assert aligner.align_long_text.call_args.args[1] == text


def test_repetitive_catalog_outro_is_rearranged_only_from_acoustic_evidence(
    tmp_path, monkeypatch
):
    catalog_lines = (
        "verse one", "verse two", "vocalise", "chorus",
        "middle one", "middle two", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
    )
    monkeypatch.setattr("AI.audio_pipeline_v2.duration", lambda _audio: 180.0)
    heard = [
        "verse", "one", "verse", "two", "vocalise", "chorus",
        "middle", "one", "middle", "two", "vocalise", "chorus",
        "vocalise", "chorus", "verse", "one", "verse", "two",
        "vocalise", "chorus",
    ]
    aligner = SimpleNamespace(
        transcribe_ctc=Mock(return_value=[
            Word(index * 0.1, index * 0.1 + 0.05, token, 0.8, index)
            for index, token in enumerate(heard)
        ]),
        set_cancelled=Mock(),
        align_long_text=Mock(side_effect=lambda _audio, text, _language: [
            Word(index * 0.1, index * 0.1 + 0.05, token, 0.9, index)
            for index, token in enumerate(text.split())
        ]),
    )
    pipeline = AudioPipelineV2(engines=SimpleNamespace(aligner=aligner))
    request = AudioPipelineV2Request(
        source_path=tmp_path / "song.flac",
        output_dir=tmp_path,
        artist="Artist",
        title="Song",
    )

    text, _words, source, score_lines = pipeline._align(
        request,
        tmp_path / "vocals.flac",
        LyricsDiscovery(
            "\n".join(catalog_lines),
            "internet",
            "query",
            lines=tuple(TimedLine(index, line) for index, line in enumerate(catalog_lines)),
        ),
    )

    assert text.splitlines()[-4:] == [
        "verse one", "verse two", "vocalise", "chorus",
    ]
    assert source == "internet+audio-arrangement"
    assert score_lines[-1].text == "chorus"
    aligner.transcribe_ctc.assert_called_once()
    aligner.align_long_text.assert_called_once()


def test_ctc_arrangement_probe_is_parked_before_forced_alignment(
    tmp_path, monkeypatch
):
    catalog_lines = (
        "verse one", "verse two", "vocalise", "chorus",
        "middle one", "middle two", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
    )
    monkeypatch.setattr("AI.audio_pipeline_v2.duration", lambda _audio: 180.0)
    heard = ["verse", "one", "verse", "two", "vocalise", "chorus", "middle", "one", "middle", "two", "vocalise", "chorus", "vocalise", "chorus", "verse", "one", "verse", "two", "vocalise", "chorus"]
    calls = Mock()
    aligner = SimpleNamespace(
        transcribe_ctc=Mock(return_value=[
            Word(index * 0.1, index * 0.1 + 0.05, token, 0.8, index)
            for index, token in enumerate(heard)
        ]),
        park=Mock(),
        set_cancelled=Mock(),
        align_long_text=Mock(side_effect=lambda _audio, text, _language: [
            Word(index * 0.1, index * 0.1 + 0.05, token, 0.9, index)
            for index, token in enumerate(text.split())
        ]),
    )
    calls.attach_mock(aligner.park, "park")
    calls.attach_mock(aligner.align_long_text, "align")
    pipeline = AudioPipelineV2(engines=SimpleNamespace(aligner=aligner))
    request = AudioPipelineV2Request(
        tmp_path / "song.flac", tmp_path, artist="Artist", title="Song"
    )

    pipeline._align(
        request,
        tmp_path / "vocals.flac",
        LyricsDiscovery("\n".join(catalog_lines), "internet", "query"),
    )

    assert aligner.park.call_count == 1
    assert calls.mock_calls.index(call.park()) < calls.mock_calls.index(
        call.align(tmp_path / "vocals.flac", ANY, None)
    )


def test_long_outro_uses_music_reprise_before_fallback_transcription(
    tmp_path, monkeypatch
):
    lines = (
        "verse one", "verse two", "vocalise", "chorus",
        "middle one", "middle two", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
    )
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.extract_music_structure",
        lambda _path: (__import__("numpy").zeros((2, 200)), 1.0, 180.0),
    )
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.find_section_reprise",
        lambda *_args, **_kwargs: SimpleNamespace(start=120.0),
    )
    aligner = SimpleNamespace(
        align_ctc_candidates=Mock(),
        transcribe_ctc=Mock(side_effect=AssertionError("music already selected")),
        set_cancelled=Mock(),
        align_long_text=Mock(side_effect=lambda _audio, text, _language: [
            Word(index * 0.1, index * 0.1 + 0.05, token, 0.9, index)
            for index, token in enumerate(text.split())
        ]),
    )
    pipeline = AudioPipelineV2(engines=SimpleNamespace(aligner=aligner))
    request = AudioPipelineV2Request(
        tmp_path / "song.flac", tmp_path, artist="Artist", title="Song"
    )

    text, _words, source, _score_lines = pipeline._align(
        request,
        tmp_path / "vocals.flac",
        LyricsDiscovery(
            "\n".join(lines), "internet", "query",
            lines=tuple(TimedLine(index * 10.0, line)
                        for index, line in enumerate(lines)),
        ),
        tmp_path / "original.flac",
    )

    assert text.splitlines()[-4:] == [
        "verse one", "verse two", "vocalise", "chorus",
    ]
    assert source == "internet+music-arrangement"
    aligner.transcribe_ctc.assert_not_called()


def test_truncated_outro_uses_partial_music_reprise_before_transcription(
    tmp_path, monkeypatch
):
    lines = (
        "verse one", "verse two", "vocalise", "chorus",
        "middle one", "middle two", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
    )
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.extract_music_structure",
        lambda _path: (__import__("numpy").zeros((20, 200)), 1.0, 180.0),
    )
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.find_section_reprise", lambda *_args, **_kwargs: None
    )
    partial = Mock(return_value=SimpleNamespace(start=120.0))
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.find_partial_section_reprise", partial
    )
    aligner = SimpleNamespace(
        transcribe_ctc=Mock(side_effect=AssertionError("music already selected")),
        set_cancelled=Mock(),
        align_timed_lines=Mock(side_effect=lambda _audio, text, _lines, _language: [
            Word(index * 0.1, index * 0.1 + 0.05, token, 0.9, index)
            for index, token in enumerate(text.split())
        ]),
        align_long_text=Mock(side_effect=AssertionError("timed tail is available")),
    )
    pipeline = AudioPipelineV2(engines=SimpleNamespace(aligner=aligner))
    request = AudioPipelineV2Request(
        tmp_path / "song.flac", tmp_path, artist="Artist", title="Song"
    )

    text, _words, source, _score_lines = pipeline._align(
        request,
        tmp_path / "vocals.flac",
        LyricsDiscovery(
            "\n".join(lines), "internet", "query",
            lines=tuple(TimedLine(index * 10.0, line)
                        for index, line in enumerate(lines)),
        ),
        tmp_path / "original.flac",
    )

    assert text.splitlines()[-4:] == [
        "verse one", "verse two", "vocalise", "chorus",
    ]
    assert source == "internet+music-arrangement"
    partial.assert_called_once()
    aligner.transcribe_ctc.assert_not_called()
    aligned_lines = aligner.align_timed_lines.call_args.args[2]
    assert [line.start for line in aligned_lines[:8]] == [
        float(index * 10) for index in range(8)
    ]


def test_ctc_word_intervals_are_made_monotonic_before_document_validation():
    words = AudioPipelineV2._normalized_words([
        Word(2.0, 2.0, "раз", 0.9, 0),
        Word(1.9, 2.1, "два", 0.9, 1),
    ])

    assert words[0].end - words[0].start > 0.009
    assert words[1].start >= words[0].start
    assert words[1].end - words[1].start > 0.009


def test_qwen_alignment_is_anchored_to_the_detected_vocal_interval(
    tmp_path: Path, monkeypatch,
):
    vocals = tmp_path / "vocals.flac"
    vocals.write_bytes(b"mock-audio")
    aligner = SimpleNamespace(
        needs_voice_anchoring=True,
        set_cancelled=Mock(),
        align_long_text=Mock(return_value=[
            Word(1.2, 1.3, "hello", 0.9, 0),
        ]),
    )
    monkeypatch.setattr(
        "AI.audio_pipeline_v2.voice_activity_intervals",
        lambda _audio: [(1.0, 2.0)],
    )
    monkeypatch.setattr("AI.audio_pipeline_v2.duration", lambda _audio: 4.0)
    pipeline = AudioPipelineV2(engines=SimpleNamespace(aligner=aligner))
    request = AudioPipelineV2Request(
        tmp_path / "song.flac", tmp_path, artist="Artist", title="Song"
    )

    _text, words, _source, _lines = pipeline._align(
        request,
        vocals,
        LyricsDiscovery("hello", "internet", "query"),
    )

    assert words[0].start == 1.0
    assert words[0].end == 2.0


def test_ctc_words_with_shared_onsets_become_strict_non_overlapping_intervals():
    words = AudioPipelineV2._normalized_words([
        Word(1.0, 1.30, "один", 0.9, 0),
        Word(1.0, 1.30, "два", 0.9, 1),
        Word(1.0, 1.30, "три", 0.9, 2),
        Word(1.25, 1.50, "четыре", 0.9, 3),
    ])

    assert all(
        words[index].end <= words[index + 1].start
        for index in range(len(words) - 1)
    )
    assert all(
        words[index].start < words[index + 1].start
        for index in range(len(words) - 1)
    )
    assert all(word.end - word.start >= 0.01 for word in words)


def test_ctc_words_keep_the_reference_minimum_visible_duration():
    words = AudioPipelineV2._normalized_words([
        Word(2.0, 2.0, "и", 0.9, 0),
        Word(2.0, 2.01, "снова", 0.9, 1),
        Word(2.02, 2.03, "поём", 0.9, 2),
    ])

    assert all(word.end - word.start >= 0.04 for word in words)
    assert all(
        words[index].end <= words[index + 1].start
        for index in range(len(words) - 1)
    )


def test_final_word_quantum_stays_inside_the_audio_duration():
    words = AudioPipelineV2._normalized_words(
        [Word(0.98, 1.0, "конец", 0.9, 0)],
        duration=1.0,
    )

    assert words[0].start == pytest.approx(0.96)
    assert words[0].end == 1.0


def test_fast_processing_does_not_run_the_four_gigabyte_symbolic_model():
    assert AudioPipelineV2._uses_symbolic_model("fast") is False
    assert AudioPipelineV2._uses_symbolic_model("auto") is False
    assert AudioPipelineV2._uses_symbolic_model("quality") is True


def test_symbolic_quality_reuses_the_fast_stem_profile():
    assert AudioPipelineV2._separation_processing_mode("quality") == "fast"
    assert AudioPipelineV2._separation_processing_mode("fast") == "fast"


def test_symbolic_quality_is_skipped_when_physical_notes_cover_the_song():
    words = [
        Word(index * 0.5, index * 0.5 + 0.4, f"word-{index}", index=index)
        for index in range(20)
    ]
    notes = [
        VocalNote(word.start, word.end, 60, word_index=word.index)
        for word in words[:-1]
    ]

    assert AudioPipelineV2._needs_symbolic_model(words, notes) is False
    assert AudioPipelineV2._needs_symbolic_model(words, notes[:10]) is True


def test_auto_processing_runs_symbolic_rescue_only_when_melody_is_missing():
    words = [
        Word(index * 0.5, index * 0.5 + 0.4, f"word-{index}", index=index)
        for index in range(20)
    ]
    healthy_notes = [
        VocalNote(word.start, word.end, 60, word_index=word.index)
        for word in words[:-2]
    ]
    single_accidental_note = [
        VocalNote(words[0].start, words[0].end, 60, word_index=words[0].index)
    ]

    assert AudioPipelineV2._should_use_symbolic_model("auto", words, []) is True
    assert (
        AudioPipelineV2._should_use_symbolic_model(
            "auto", words, single_accidental_note
        )
        is True
    )
    assert (
        AudioPipelineV2._should_use_symbolic_model("auto", words, healthy_notes)
        is False
    )


def test_audio_processing_rejects_complete_lyrics_without_any_melody_notes():
    words = [Word(1.0, 1.5, "песня", index=0)]

    with pytest.raises(EngineUnavailableError, match="мелодическ"):
        AudioPipelineV2._require_complete_melody(words, [])


def test_audio_processing_rejects_result_without_synchronized_words():
    with pytest.raises(EngineUnavailableError, match="синхронизирован"):
        AudioPipelineV2._require_complete_melody([], [])


def test_audio_processing_rejects_catastrophically_sparse_rescue_result():
    words = [
        Word(index * 0.5, index * 0.5 + 0.4, f"word-{index}", index=index)
        for index in range(20)
    ]
    notes = [VocalNote(0.0, 0.4, 60, word_index=0)]

    with pytest.raises(EngineUnavailableError, match="мелодическ"):
        AudioPipelineV2._require_complete_melody(words, notes)


def test_fast_processing_keeps_small_analysis_models_warm_between_songs():
    assert AudioPipelineV2._keeps_analysis_models_warm("fast") is True
    assert AudioPipelineV2._keeps_analysis_models_warm("auto") is True
    assert AudioPipelineV2._keeps_analysis_models_warm("quality") is False


def test_separator_is_released_before_alignment_to_avoid_gpu_contention():
    assert AudioPipelineV2._keeps_separator_warm("fast") is False
    assert AudioPipelineV2._keeps_separator_warm("auto") is False
    assert AudioPipelineV2._keeps_separator_warm("quality") is False
