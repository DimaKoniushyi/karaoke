from __future__ import annotations

import json
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
from AI.lyrics_sources import LyricsDiscovery, TimedLine
from AI.models import VocalNote, Word


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
            "words": [{"text": "тест", "start": 0.1, "end": 0.2, "notes": []}],
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
    aligner.align_long_text.assert_called_once_with(
        tmp_path / "vocals.flac", "Первая вторая", None
    )


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


def test_repetitive_catalog_outro_is_rearranged_only_from_acoustic_evidence(tmp_path):
    catalog_lines = (
        "verse one", "verse two", "vocalise", "chorus",
        "middle one", "middle two", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
    )
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


def test_ctc_arrangement_probe_is_parked_before_forced_alignment(tmp_path):
    catalog_lines = (
        "verse one", "verse two", "vocalise", "chorus",
        "middle one", "middle two", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
        "vocalise", "chorus", "vocalise", "chorus",
    )
    heard = (
        "verse one verse two vocalise chorus middle one middle two "
        "vocalise chorus vocalise chorus verse one verse two vocalise chorus"
    ).split()
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


def test_ctc_word_intervals_are_made_monotonic_before_document_validation():
    words = AudioPipelineV2._normalized_words([
        Word(2.0, 2.0, "раз", 0.9, 0),
        Word(1.9, 2.1, "два", 0.9, 1),
    ])

    assert words[0].end - words[0].start > 0.009
    assert words[1].start >= words[0].start
    assert words[1].end - words[1].start > 0.009


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


def test_fast_processing_keeps_small_analysis_models_warm_between_songs():
    assert AudioPipelineV2._keeps_analysis_models_warm("fast") is True
    assert AudioPipelineV2._keeps_analysis_models_warm("auto") is True
    assert AudioPipelineV2._keeps_analysis_models_warm("quality") is False


def test_separator_is_released_before_alignment_to_avoid_gpu_contention():
    assert AudioPipelineV2._keeps_separator_warm("fast") is False
    assert AudioPipelineV2._keeps_separator_warm("auto") is False
    assert AudioPipelineV2._keeps_separator_warm("quality") is False
