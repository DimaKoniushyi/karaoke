import json
import os
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pytest
import soundfile as sf

import AI.pipeline as pipeline_module
from AI.artifacts import publish_files_atomically
from AI.engines.separation import MSSTMelRoformerSeparator
from AI.engines.text import (
    Qwen3ForcedAligner,
    _acoustic_runs,
    _enforce_monotonic_starts,
    _fill_unresolved_timed_lines,
    _invalid_runs,
    _prepare_greedy_generation,
    _repair_collapsed_timed_lines,
    _repair_timed_line_outliers,
    _timed_line_offset,
    _timed_line_plan,
    _timed_line_retry_stages,
    resolve_alignment_language,
    tokenize,
)
from AI.engines.timed_alignment import coarse_line_starts
from AI.errors import (
    AlignmentTimeoutError,
    EngineUnavailableError,
    InvalidArtifactError,
    ProcessingCancelledError,
)
from AI.lyrics_sources import (
    LyricsDiscovery,
    TimedLine,
    _expand_notation,
    _lyrics_arrangement_candidates,
    _retime_arrangement_lines,
    _select_complete_lyrics,
    _select_lyrics_arrangement,
    _select_reprise_candidate_at_time,
    _select_scored_lyrics_arrangement,
    discover_lyrics,
)
from AI.models import PitchFrame, Word
from AI.pipeline import KaraokePipeline, PipelineRequest


class Separator:
    name = "test-separator"

    @staticmethod
    def separate(mix, vocals, instrumental, *, profile=None, cancelled=None):
        audio, rate = sf.read(mix, dtype="float32", always_2d=True)
        sf.write(vocals, audio, rate)
        sf.write(instrumental, audio * 0, rate)

    @staticmethod
    def close():
        return None


def test_greedy_asr_generation_has_explicit_padding_and_no_sampling_flags():
    generation = SimpleNamespace(
        do_sample=False,
        temperature=0.6,
        top_p=0.9,
        top_k=20,
        pad_token_id=None,
        eos_token_id=42,
    )
    nested_generation = SimpleNamespace(
        do_sample=False,
        temperature=1.0,
        top_p=1.0,
        top_k=50,
        pad_token_id=None,
        eos_token_id=None,
    )
    nested = SimpleNamespace(
        generation_config=nested_generation,
        config=SimpleNamespace(pad_token_id=None, eos_token_id=None),
    )
    wrapper = SimpleNamespace(
        backend="transformers",
        model=SimpleNamespace(
            generation_config=generation,
            config=SimpleNamespace(pad_token_id=None, eos_token_id=42),
            thinker=nested,
        ),
    )

    _prepare_greedy_generation(wrapper)

    assert generation.temperature is None
    assert generation.top_p is None
    assert generation.top_k is None
    assert generation.pad_token_id == 42
    assert wrapper.model.config.pad_token_id == 42
    assert nested_generation.temperature is None
    assert nested_generation.top_p is None
    assert nested_generation.top_k is None
    assert nested_generation.pad_token_id == 42
    assert nested.config.pad_token_id == 42


def test_model_parking_unloads_on_memory_constrained_computers(monkeypatch):
    engine = SimpleNamespace(close=Mock(), park=Mock())
    memory = SimpleNamespace(total=16 * 1024**3, available=5 * 1024**3)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(virtual_memory=lambda: memory))
    monkeypatch.setattr(pipeline_module, "release_torch_memory", lambda: None)

    KaraokePipeline._park_engines(engine)

    engine.close.assert_called_once_with()
    engine.park.assert_not_called()


def test_model_parking_keeps_warm_models_only_with_ram_headroom(monkeypatch):
    engine = SimpleNamespace(
        close=Mock(),
        park=Mock(),
    )
    memory = SimpleNamespace(total=64 * 1024**3, available=20 * 1024**3)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(virtual_memory=lambda: memory))
    monkeypatch.setattr(pipeline_module, "release_torch_memory", lambda: None)

    KaraokePipeline._park_engines(engine)

    engine.park.assert_called_once_with()
    engine.close.assert_not_called()


class Pitch:
    name = "test-pitch"

    @staticmethod
    def estimate(_audio):
        return [PitchFrame(time / 100, 440, 1, True) for time in range(100, 151)]


class Aligner:
    name = "test-aligner"

    @staticmethod
    def align_long_text(_audio, text, _language):
        return [Word(1, 1.6, text, index=0)]


def test_clean_pipeline_publishes_only_canonical_runtime_artifacts(tmp_path, monkeypatch):
    source, lyrics, output = tmp_path / "source.wav", tmp_path / "lyrics.txt", tmp_path / "out"
    sf.write(source, np.zeros((44100 * 2, 2), dtype=np.float32), 44100)
    lyrics.write_text("hello", encoding="utf-8")
    monkeypatch.setattr("AI.pipeline.analyze_music", lambda _path: {"bpm": 120, "key": "A minor"})
    engines = SimpleNamespace(
        separator=Separator(), pitch=Pitch(), transcriber=None, aligner=Aligner()
    )

    result = KaraokePipeline(engines=engines).run(
        PipelineRequest(source, output, lyrics_path=lyrics)
    )

    assert result.manifest_path == output / "lyricsSync.json"
    assert {path.name for path in output.iterdir()} == {
        "instrumental.flac",
        "lyricsSync.json",
        "vocals.flac",
    }
    assert sf.info(output / "vocals.flac").channels == 1


def test_pipeline_releases_separator_before_loading_later_gpu_models(tmp_path, monkeypatch):
    source, lyrics, output = tmp_path / "source.wav", tmp_path / "lyrics.txt", tmp_path / "out"
    sf.write(source, np.zeros((44100 * 2, 2), dtype=np.float32), 44100)
    lyrics.write_text("hello", encoding="utf-8")
    monkeypatch.setattr("AI.pipeline.analyze_music", lambda _path: {"bpm": 120, "key": "A minor"})
    events = []

    class ReleasableSeparator(Separator):
        @staticmethod
        def separate(*args, **kwargs):
            Separator.separate(*args, **kwargs)
            events.append("separated")

        @staticmethod
        def close():
            events.append("released")

    class CheckedPitch(Pitch):
        @staticmethod
        def estimate(audio):
            assert events == ["separated", "released"]
            return Pitch.estimate(audio)

    engines = SimpleNamespace(
        separator=ReleasableSeparator(),
        pitch=CheckedPitch(),
        transcriber=None,
        aligner=Aligner(),
    )

    KaraokePipeline(engines=engines).run(PipelineRequest(source, output, lyrics_path=lyrics))

    assert events == ["separated", "released"]


def test_audio_pipeline_auto_mode_uses_the_fast_profile(tmp_path, monkeypatch):
    source, lyrics, output = tmp_path / "source.wav", tmp_path / "lyrics.txt", tmp_path / "out"
    sf.write(source, np.zeros((44100 * 2, 2), dtype=np.float32), 44100)
    lyrics.write_text("hello", encoding="utf-8")
    monkeypatch.setattr("AI.pipeline.analyze_music", lambda _path: {"bpm": 120, "key": "A minor"})
    profiles = []

    class ProfileSeparator(Separator):
        @staticmethod
        def separate(*args, profile=None, **kwargs):
            profiles.append(profile)
            Separator.separate(*args, profile=profile, **kwargs)

    engines = SimpleNamespace(
        separator=ProfileSeparator(), pitch=Pitch(), transcriber=None, aligner=Aligner()
    )

    KaraokePipeline(engines=engines).run(PipelineRequest(source, output, lyrics_path=lyrics))

    assert profiles[0].mode == "fast"


def test_pipeline_keeps_full_ctc_word_boundaries_without_global_vad_reanchoring(
    tmp_path, monkeypatch
):
    source, lyrics, output = tmp_path / "source.wav", tmp_path / "lyrics.txt", tmp_path / "out"
    sf.write(source, np.zeros((44100 * 2, 2), dtype=np.float32), 44100)
    lyrics.write_text("first second", encoding="utf-8")
    monkeypatch.setattr("AI.pipeline.analyze_music", lambda _path: {"bpm": 120, "key": "A minor"})
    monkeypatch.setattr(
        "AI.pipeline.anchor_words_to_voice",
        lambda *_args: (_ for _ in ()).throw(AssertionError("CTC boundaries must stay intact")),
    )
    aligner = SimpleNamespace(
        name="ctc-full",
        needs_voice_anchoring=False,
        align_long_text=lambda *_args: [
            Word(0.2, 0.6, "first", index=0),
            Word(1.1, 1.6, "second", index=1),
        ],
    )
    engines = SimpleNamespace(
        separator=Separator(), pitch=Pitch(), transcriber=None, aligner=aligner
    )

    KaraokePipeline(engines=engines).run(PipelineRequest(source, output, lyrics_path=lyrics))

    payload = __import__("json").loads((output / "lyricsSync.json").read_text(encoding="utf-8"))
    assert [(word["start"], word["end"]) for word in payload["words"]] == [
        (0.2, 0.6),
        (1.1, 1.6),
    ]


def test_stage_reports_carry_device_dtype_and_memory_telemetry(tmp_path, monkeypatch):
    from AI.runtime import BackendSpec, HardwareProfile, RuntimePlan

    source, lyrics, output = tmp_path / "source.wav", tmp_path / "lyrics.txt", tmp_path / "out"
    sf.write(source, np.zeros((44100 * 2, 2), dtype=np.float32), 44100)
    lyrics.write_text("hello", encoding="utf-8")
    monkeypatch.setattr("AI.pipeline.analyze_music", lambda _path: {"bpm": 120, "key": "A minor"})
    plan = RuntimePlan(
        HardwareProfile("cpu", 4),
        {
            "separation": BackendSpec("pytorch:cuda", "cuda", "fp16"),
            "pitch": BackendSpec("pytorch:cpu", "cpu", "fp32"),
            "aligner": BackendSpec("pytorch:cpu", "cpu", "fp32"),
        },
    )
    monkeypatch.setattr("AI.pipeline.get_runtime_plan", lambda: plan)
    engines = SimpleNamespace(
        separator=Separator(), pitch=Pitch(), transcriber=None, aligner=Aligner()
    )

    result = KaraokePipeline(engines=engines).run(
        PipelineRequest(source, output, lyrics_path=lyrics)
    )

    by_stage = {report.stage: report for report in result.reports}
    assert (by_stage["separate"].details["device"], by_stage["separate"].details["dtype"]) == (
        "cuda",
        "fp16",
    )
    assert (by_stage["pitch"].details["device"], by_stage["pitch"].details["dtype"]) == (
        "cpu",
        "fp32",
    )
    assert (by_stage["lyrics"].details["device"], by_stage["lyrics"].details["dtype"]) == (
        "cpu",
        "fp32",
    )
    # decode has no backend role, but every stage still reports process memory use.
    assert all(report.details.get("rss_bytes", 0) > 0 for report in result.reports)
    assert "device" not in by_stage["decode"].details


def test_reprocess_rebuilds_timing_from_existing_vocals_without_separation(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    sf.write(output / "vocals.flac", np.zeros(44100 * 2, dtype=np.float32), 44100)
    (output / "instrumental.flac").write_bytes(b"minus stays untouched")
    (output / "lyricsSync.json").write_text(
        '{"bpm":120,"key":"A minor","text":"hello","words":['
        '{"start":0.1,"end":0.2,"text":"hello","notes":[]}]}',
        encoding="utf-8",
    )
    engines = SimpleNamespace(
        separator=Separator(), pitch=Pitch(), transcriber=None, aligner=Aligner()
    )

    result = KaraokePipeline(engines=engines).reprocess(output, language="en")

    assert result.manifest_path == output / "lyricsSync.json"
    assert (output / "instrumental.flac").read_bytes() == b"minus stays untouched"
    assert '"start": 1.0' in (output / "lyricsSync.json").read_text(encoding="utf-8")


def test_reprocess_reuses_matching_online_timed_lines(tmp_path, monkeypatch):
    output = tmp_path / "out"
    output.mkdir()
    sf.write(output / "vocals.flac", np.zeros(44100 * 4, dtype=np.float32), 44100)
    (output / "lyricsSync.json").write_text(
        '{"bpm":120,"key":"A minor","text":"right line\\nnext line","words":['
        '{"start":0.1,"end":0.2,"text":"right","notes":[]},'
        '{"start":0.2,"end":0.3,"text":"line","notes":[]},'
        '{"start":0.3,"end":0.4,"text":"next","notes":[]},'
        '{"start":0.4,"end":0.5,"text":"line","notes":[]}]}',
        encoding="utf-8",
    )
    timed_lines = (TimedLine(1.0, "right line"), TimedLine(3.0, "next line"))
    monkeypatch.setattr(
        "AI.pipeline.discover_lyrics",
        lambda _title, _artist=None: LyricsDiscovery(
            "right line\nnext line", "LRCLIB", "Artist Song", lines=timed_lines
        ),
    )

    class TimedAligner(Aligner):
        @staticmethod
        def align_timed_lines(_audio, _text, lines, _language):
            assert lines == timed_lines
            return [
                Word(1.0, 1.4, "right", index=0),
                Word(1.4, 1.8, "line", index=1),
                Word(3.0, 3.4, "next", index=2),
                Word(3.4, 3.8, "line", index=3),
            ]

        @staticmethod
        def align_long_text(*_args):
            raise AssertionError("full-song alignment must not run")

    engines = SimpleNamespace(
        separator=Separator(), pitch=Pitch(), transcriber=None, aligner=TimedAligner()
    )

    KaraokePipeline(engines=engines).reprocess(output, title="Artist - Song")

    payload = __import__("json").loads((output / "lyricsSync.json").read_text(encoding="utf-8"))
    assert [word["start"] for word in payload["words"]] == [1.0, 1.4, 3.0, 3.4]


def test_atomic_publish_restores_every_previous_artifact(tmp_path, monkeypatch):
    sources = [tmp_path / f"new-{index}" for index in range(2)]
    targets = [tmp_path / f"target-{index}" for index in range(2)]
    for index, path in enumerate(sources):
        path.write_text(f"new-{index}", encoding="utf-8")
    for index, path in enumerate(targets):
        path.write_text(f"old-{index}", encoding="utf-8")
    real_replace = __import__("os").replace

    def fail_second_publish(source, target):
        if source == sources[1] and target == targets[1]:
            raise OSError("disk failure")
        real_replace(source, target)

    monkeypatch.setattr("AI.artifacts.os.replace", fail_second_publish)

    with pytest.raises(OSError, match="disk failure"):
        publish_files_atomically(list(zip(sources, targets, strict=True)))

    assert [path.read_text(encoding="utf-8") for path in targets] == ["old-0", "old-1"]


def test_lyrics_tokens_match_qwen_space_language_contract():
    assert tokenize("Ты станешь слаще —\nА я, кто-то…") == [
        "Ты",
        "станешь",
        "слаще",
        "А",
        "я",
        "кто-то",
    ]
    assert resolve_alignment_language("текст", "ru-RU") == "Russian"
    assert resolve_alignment_language("український текст") == "Ukrainian"
    assert resolve_alignment_language("український текст", "ru") == "Ukrainian"


def test_catalog_text_repairs_mixed_cyrillic_homoglyphs_and_sung_repetition():
    import AI.engines.text as text_engine

    source = "Hа дачу\nОна жуёт свой оpбит\nА ты жуй-жуй\nDuke Nukem"

    cleaned = text_engine.normalize_lyrics_text(source)

    assert cleaned == "На дачу\nОна жуёт свой орбит\nА ты жуй жуй\nDuke Nukem"
    assert tokenize(source) == [
        "На",
        "дачу",
        "Она",
        "жуёт",
        "свой",
        "орбит",
        "А",
        "ты",
        "жуй",
        "жуй",
        "Duke",
        "Nukem",
    ]


def test_catalog_text_splits_a_sung_vocalisation_but_keeps_a_lexical_hyphen():
    import AI.engines.text as text_engine

    cleaned = text_engine.normalize_lyrics_text("О-оу-и-я-и-ё\nчто-то")

    assert cleaned == "О оу ия иё\nчто-то"
    assert tokenize(cleaned) == ["О", "оу", "ия", "иё", "что-то"]


def test_catalog_text_merges_vowels_only_inside_a_vocalisation_line():
    import AI.engines.text as text_engine

    cleaned = text_engine.normalize_lyrics_text("О оу и я и ё\nИ я вернусь")

    assert cleaned == "О оу ия иё\nИ я вернусь"


def test_lyrics_discovery_falls_back_to_verified_ukrainian_catalog(monkeypatch):
    search = '<a href="/songs/42.html">Лови момент</a>'
    detail = """
        <h1>Лови момент</h1>
        <a href="/persons/7.html">АнтитілА</a>
        <pre class="songwords">Куплет один тут є\nПриспів:\nРядок приспіву один\nРядок приспіву два\n\nПриспів.</pre>
    """

    def response(url, encoding="utf-8"):
        del encoding
        if "lrclib.net" in url:
            return "[]"
        return detail if "/songs/42.html" in url else search

    monkeypatch.setattr("AI.lyrics_sources._request", response)

    result = discover_lyrics("Лови момент", "Антитiла")

    assert result is not None
    assert result.source == "pisni.org.ua"
    assert result.text.count("Рядок приспіву один") == 2


def test_lrclib_synced_text_is_kept_with_its_line_timestamps(monkeypatch):
    monkeypatch.setattr(
        "AI.lyrics_sources._request",
        lambda _url, _encoding="utf-8": (
            """[{"trackName":"Song","artistName":"Artist","plainLyrics":"wrong text","syncedLyrics":"[00:01.00]right line\\n[00:03.00]next line"}]"""
        ),
    )

    result = discover_lyrics("Song", "Artist")

    assert result is not None
    assert result.text == "right line\nnext line"
    assert result.lines == (TimedLine(1.0, "right line"), TimedLine(3.0, "next line"))


def test_lrclib_exact_duration_lookup_runs_before_fuzzy_search(monkeypatch):
    requested = []

    def response(url, _encoding="utf-8"):
        requested.append(url)
        return json.dumps({
            "trackName": "Song",
            "artistName": "Artist",
            "plainLyrics": "complete exact lyrics from the matching recording",
            "syncedLyrics": "",
        })

    monkeypatch.setattr("AI.lyrics_sources._request", response)

    result = discover_lyrics("Song", "Artist", duration_seconds=123.4)

    assert result is not None
    assert result.text == "complete exact lyrics from the matching recording"
    assert len(requested) == 1
    assert "/api/get?" in requested[0]
    assert "duration=123" in requested[0]


def test_lyrics_selection_prefers_a_longer_compatible_song_structure():
    short = LyricsDiscovery(
        "первый куплет\nприпев\nвторой куплет\nприпев",
        "timed",
        "Artist - Song",
        lines=(TimedLine(1.0, "первый куплет"),),
    )
    complete = LyricsDiscovery(
        "первый куплет\nприпев\nвторой куплет\nприпев\nпервый куплет\nприпев",
        "catalog",
        "Artist - Song",
    )

    selected = _select_complete_lyrics((short, complete))

    assert selected is complete


def test_lyrics_selection_rejects_a_longer_unrelated_version():
    exact = LyricsDiscovery(
        "точный первый куплет и точный припев",
        "timed",
        "Artist - Song",
    )
    unrelated = LyricsDiscovery(
        "совсем другие слова другой концертной версии и длинный комментарий",
        "catalog",
        "Artist - Song",
    )

    assert _select_complete_lyrics((exact, unrelated)) is exact


def test_lyrics_arrangements_offer_a_verse_reprise_for_a_repetitive_outro():
    lines = (
        "verse one",
        "verse two",
        "vocalise",
        "chorus",
        "middle one",
        "middle two",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
    )

    candidates = _lyrics_arrangement_candidates(lines)

    assert candidates[0] == lines
    assert len(candidates) <= 4
    assert any(
        candidate[-4:] == ("verse one", "verse two", "vocalise", "chorus")
        for candidate in candidates[1:]
    )


def test_lyrics_arrangements_do_not_invent_a_reprise_without_a_long_outro():
    lines = ("verse one", "verse two", "chorus", "final line")

    assert _lyrics_arrangement_candidates(lines) == (lines,)


def test_lyrics_arrangement_uses_the_structure_heard_in_the_recording():
    catalog = (
        "verse one",
        "verse two",
        "vocalise",
        "chorus",
        "middle one",
        "middle two",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
    )
    candidates = _lyrics_arrangement_candidates(catalog)
    heard = (
        "verse one verse two vocalise chorus middle one middle two "
        "vocalise chorus vocalise chorus verse one verse two vocalise chorus"
    )

    selected = _select_lyrics_arrangement(candidates, heard)

    assert selected[-4:] == ("verse one", "verse two", "vocalise", "chorus")


def test_lyrics_arrangement_keeps_catalog_when_audio_evidence_is_weak():
    catalog = ("verse one", "chorus", "vocalise", "chorus")
    alternative = ("verse one", "chorus", "verse one", "chorus")

    assert (
        _select_lyrics_arrangement((catalog, alternative), "unrelated uncertain words") == catalog
    )


def test_scored_lyrics_arrangement_requires_a_real_ctc_improvement():
    original = ("verse", "chorus", "chorus", "chorus", "chorus")
    reprise = ("verse", "chorus", "verse", "chorus")

    assert _select_scored_lyrics_arrangement((original, reprise), (-1.20, -0.72)) == 1
    assert _select_scored_lyrics_arrangement((original, reprise), (-0.75, -0.73)) == 0


def test_reprise_time_selects_the_matching_number_of_outro_repeats():
    lines = (
        "verse one",
        "verse two",
        "vocalise",
        "chorus",
        "middle one",
        "middle two",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
        "vocalise",
        "chorus",
    )
    starts = (
        0,
        5,
        10,
        13,
        16,
        20,
        30,
        33,
        40,
        43,
        50,
        53,
        60,
        63,
        90,
        93,
        100,
        103,
        110,
        113,
        120,
        123,
    )
    timed = tuple(TimedLine(float(start), line) for start, line in zip(starts, lines, strict=True))
    candidates = _lyrics_arrangement_candidates(lines)

    selected = _select_reprise_candidate_at_time(timed, candidates, 110.2)

    assert selected > 0
    assert candidates[selected][-4:] == (
        "verse one",
        "verse two",
        "vocalise",
        "chorus",
    )


def test_retimed_arrangement_preserves_common_prefix_and_places_only_new_tail():
    original = (
        TimedLine(10.0, "first line"),
        TimedLine(20.0, "second line"),
        TimedLine(30.0, "old ending"),
    )
    selected = ("first line", "second line", "new words here", "last")

    result = _retime_arrangement_lines(original, selected, duration=50.0)

    assert [line.text for line in result] == list(selected)
    assert [line.start for line in result[:2]] == [10.0, 20.0]
    assert result[2].start == 30.0
    assert result[3].start == 45.0


def test_complete_lyrics_lookup_compares_lrclib_with_the_plain_catalog(monkeypatch):
    lrclib = json.dumps(
        [
            {
                "trackName": "Song",
                "artistName": "Artist",
                "syncedLyrics": (
                    "[00:01.00]first verse has several exact words\n[00:05.00]the chorus stays here"
                ),
            }
        ]
    )
    complete_body = (
        "first verse has several exact words\n"
        "the chorus stays here\n"
        "second verse adds the missing ending\n"
        "the chorus stays here"
    )

    def response(url, _encoding="utf-8"):
        if "lrclib.net" in url:
            return lrclib
        if "musixmatch.com" in url:
            return json.dumps({"lyrics": {"body": complete_body}})
        raise AssertionError(url)

    monkeypatch.setattr("AI.lyrics_sources._request", response)

    result = discover_lyrics("Song", "Artist", complete=True)

    assert result is not None
    assert result.text == complete_body
    assert result.source == "Musixmatch"
    assert result.lines == ()


def test_complete_lyrics_lookup_uses_plain_catalog_when_lrclib_has_no_song(monkeypatch):
    complete_body = (
        "first verse has several exact words\n"
        "the chorus stays here\n"
        "second verse adds the missing ending"
    )

    def response(url, _encoding="utf-8"):
        if "lrclib.net" in url:
            return "[]"
        if "musixmatch.com" in url:
            return json.dumps({"lyrics": {"body": complete_body}})
        raise AssertionError(url)

    monkeypatch.setattr("AI.lyrics_sources._request", response)
    monkeypatch.setattr("AI.lyrics_sources._pisni", lambda *_args: None)

    result = discover_lyrics("Song", "Artist", complete=True)

    assert result is not None
    assert result.text == complete_body
    assert result.source == "Musixmatch"


def test_lrclib_drops_an_incomplete_final_repetition_block(monkeypatch):
    synced = "\n".join(
        (
            "[00:01.00]Куплет начинается",
            "[00:10.00]Припев первая строка",
            "[00:14.00]Припев вторая строка полностью",
            "[00:20.00]Припев первая строка",
            "[00:24.00]Припев вторая строка полностью",
            "[00:30.00]Припев первая строка",
            "[00:34.00]Припев вторая",
        )
    )
    payload = json.dumps(
        [
            {
                "trackName": "Song",
                "artistName": "Artist",
                "plainLyrics": "unused",
                "syncedLyrics": synced,
            }
        ],
        ensure_ascii=False,
    )
    monkeypatch.setattr(
        "AI.lyrics_sources._request",
        lambda _url, _encoding="utf-8": payload,
    )

    result = discover_lyrics("Song", "Artist")

    assert result is not None
    assert result.text.splitlines() == [
        "Куплет начинается",
        "Припев первая строка",
        "Припев вторая строка полностью",
        "Припев первая строка",
        "Припев вторая строка полностью",
    ]
    assert [line.start for line in result.lines] == [1.0, 10.0, 14.0, 20.0, 24.0]


def test_timed_alignment_repairs_a_line_final_word_collapsed_onto_next_line():
    tokens = ["Он", "не", "сошёл", "с", "ума", "Ты", "знала"]
    words = [
        Word(1.0, 1.2, "Он", 0.9, 0),
        Word(1.2, 1.4, "не", 0.9, 1),
        Word(1.4, 2.0, "сошёл", 0.9, 2),
        Word(2.0, 2.1, "с", 0.9, 3),
        Word(5.95, 6.1, "ума", 0.2, 4),
        Word(6.0, 6.3, "Ты", 0.9, 5),
        Word(6.3, 7.0, "знала", 0.9, 6),
    ]

    _repair_timed_line_outliers(words, [(1.0, 0, 5), (6.0, 5, 7)], tokens, 8.0)

    assert words[4].start == words[3].start
    assert words[4].end <= 6.0


def test_timed_alignment_reuses_a_stable_repeated_line_shape_for_a_gross_outlier():
    tokens = ["Припев", "никто", "не", "ждёт"] * 2
    words = [
        Word(10.0, 11.0, "Припев", 0.9, 0),
        Word(11.0, 15.0, "никто", 0.9, 1),
        Word(15.0, 16.0, "не", 0.9, 2),
        Word(16.0, 18.0, "ждёт", 0.9, 3),
        Word(30.0, 36.0, "Припев", 0.9, 4),
        Word(36.0, 44.0, "никто", 0.2, 5),
        Word(44.0, 45.0, "не", 0.2, 6),
        Word(45.0, 47.0, "ждёт", 0.2, 7),
    ]

    _repair_timed_line_outliers(words, [(10.0, 0, 4), (30.0, 4, 8)], tokens, 50.0)

    assert [word.start for word in words[4:]] == [30.0, 31.0, 35.0, 36.0]


def test_timed_alignment_does_not_let_a_repeated_line_claim_the_instrumental_gap():
    tokens = ["Нэнси", "и", "Сид", "Нэнси", "и", "Сид"]
    words = [
        Word(10.0, 10.4, "Нэнси", 0.9, 0),
        Word(10.4, 10.6, "и", 0.9, 1),
        Word(10.6, 10.9, "Сид", 0.9, 2),
        Word(30.0, 30.4, "Нэнси", 0.9, 3),
        Word(30.4, 30.6, "и", 0.9, 4),
        Word(30.6, 47.0, "Сид", 0.9, 5),
    ]

    _repair_timed_line_outliers(
        words,
        [(10.0, 0, 3), (30.0, 3, 6)],
        tokens,
        50.0,
    )

    assert words[5].end == pytest.approx(30.9)


def test_timed_alignment_uses_a_complete_line_for_its_truncated_repeated_prefix():
    tokens = [
        "Как",
        "Сид",
        "и",
        "Нэнси",
        "Сид",
        "и",
        "Нэнси",
        "Мы",
        "Сид",
        "и",
        "Нэнси",
        "Сид",
        "и",
    ]
    words = [
        Word(10.0, 10.3, "Как", 0.9, 0),
        Word(10.3, 10.6, "Сид", 0.9, 1),
        Word(10.6, 10.8, "и", 0.9, 2),
        Word(10.8, 11.3, "Нэнси", 0.9, 3),
        Word(11.3, 11.6, "Сид", 0.9, 4),
        Word(11.6, 11.8, "и", 0.9, 5),
        Word(11.8, 12.3, "Нэнси", 0.9, 6),
        Word(30.0, 30.3, "Мы", 0.9, 7),
        Word(30.3, 30.6, "Сид", 0.9, 8),
        Word(30.6, 30.8, "и", 0.9, 9),
        Word(30.8, 31.3, "Нэнси", 0.9, 10),
        Word(31.3, 47.0, "Сид", 0.9, 11),
        Word(47.0, 47.01, "и", 0.1, 12),
    ]

    _repair_timed_line_outliers(
        words,
        [(10.0, 0, 7), (30.0, 7, 13)],
        tokens,
        50.0,
    )

    assert words[11].end == pytest.approx(31.6)
    assert words[12].end == pytest.approx(31.8)


def test_timed_alignment_separates_duplicate_word_onsets_inside_one_line():
    tokens = ["Ты", "ничего", "не"]
    words = [
        Word(59.4, 59.7, "Ты", 0.4, 0),
        Word(59.4, 60.4, "ничего", 0.4, 1),
        Word(60.4, 60.8, "не", 0.9, 2),
    ]

    _repair_timed_line_outliers(words, [(59.0, 0, 3)], tokens, 61.0)

    assert words[0].start == 59.4
    assert words[0].end == words[1].start
    assert 59.4 < words[1].start < 60.4
    assert words[1].end == 60.4


def test_lrclib_uses_exact_artist_and_title_fields(monkeypatch):
    requested = []

    def response(url, _encoding="utf-8"):
        requested.append(url)
        return """[{"trackName":"Романс","artistName":"Сплин",\
"plainLyrics":"Это достаточно длинный правильный текст песни для обработки",\
"syncedLyrics":"[00:01.00]Это достаточно длинный\\n[00:04.00]правильный текст песни для обработки"}]"""

    monkeypatch.setattr("AI.lyrics_sources._request", response)

    result = discover_lyrics("Романс", "Сплин")

    assert result is not None
    assert result.source == "LRCLIB"
    assert result.query == "Сплин - Романс"
    assert result.lines[0] == TimedLine(1.0, "Это достаточно длинный")
    assert len(requested) == 1


def test_lrclib_does_not_accept_unrelated_exact_artist(monkeypatch):
    monkeypatch.setattr(
        "AI.lyrics_sources._request",
        lambda _url, _encoding="utf-8": (
            """[{"trackName":"Романс",\
"artistName":"Другая группа","plainLyrics":"Совершенно посторонний текст"}]"""
        ),
    )
    monkeypatch.setattr("AI.lyrics_sources._pisni", lambda *_args: None)

    assert discover_lyrics("Романс", "Сплин") is None


def test_lrclib_ignores_provider_version_suffix_when_title_itself_matches(monkeypatch):
    monkeypatch.setattr(
        "AI.lyrics_sources._request",
        lambda _url, _encoding="utf-8": (
            """[{"trackName":"Лесник (Из сериала Король и Шут)",\
"artistName":"Король и Шут","plainLyrics":"Достаточно длинный текст оригинальной песни"}]"""
        ),
    )

    result = discover_lyrics("Лесник", "Король и Шут")

    assert result is not None


def test_lrclib_retries_exact_title_when_provider_spells_artist_in_another_script(monkeypatch):
    requested = []

    def response(url, _encoding="utf-8"):
        requested.append(url)
        if "artist_name=" in url:
            return "[]"
        return """[{"trackName":"Сид и Нэнси","artistName":"Lumen",\
"plainLyrics":"Достаточно длинный точный текст песни для дальнейшей обработки"}]"""

    monkeypatch.setattr("AI.lyrics_sources._request", response)

    result = discover_lyrics("Сид и Нэнси", "Люмен")

    assert result is not None
    assert len(requested) == 2
    assert "track_name=" in requested[1] and "artist_name=" not in requested[1]


def test_pipeline_uses_online_lyrics_before_acoustic_transcription(tmp_path, monkeypatch):
    source, output = tmp_path / "source.wav", tmp_path / "out"
    sf.write(source, np.zeros((44100 * 4, 2), dtype=np.float32), 44100)
    monkeypatch.setattr("AI.pipeline.analyze_music", lambda _path: {"bpm": 120, "key": "A minor"})
    timed_lines = (TimedLine(1.0, "right line"), TimedLine(3.0, "next line"))
    monkeypatch.setattr(
        "AI.pipeline.discover_lyrics",
        lambda _title, _artist=None: LyricsDiscovery(
            "right line\nnext line", "LRCLIB", "Artist Song", lines=timed_lines
        ),
    )
    transcriber = SimpleNamespace(
        transcribe=Mock(side_effect=AssertionError("ASR must remain a last-resort fallback")),
        close=Mock(),
    )

    class TimedAligner(Aligner):
        @staticmethod
        def align_timed_lines(_audio, _text, _lines, _language):
            return [
                Word(1.0, 1.4, "right", index=0),
                Word(1.4, 1.8, "line", index=1),
                Word(3.0, 3.4, "next", index=2),
                Word(3.4, 3.8, "line", index=3),
            ]

    engines = SimpleNamespace(
        separator=Separator(), pitch=Pitch(), transcriber=transcriber, aligner=TimedAligner()
    )

    KaraokePipeline(engines=engines).run(PipelineRequest(source, output, title="Artist Song"))

    transcriber.transcribe.assert_not_called()


def test_lyrics_lookup_retries_exact_metadata_once_before_asr(monkeypatch):
    found = LyricsDiscovery(
        "right line", "LRCLIB", "Artist - Song", lines=(TimedLine(1.0, "right line"),)
    )
    monkeypatch.setattr("AI.pipeline.discover_lyrics", Mock(return_value=found))
    transcriber = SimpleNamespace(
        transcribe=Mock(side_effect=AssertionError("ASR must not run after a successful retry"))
    )
    pipeline = KaraokePipeline(engines=SimpleNamespace(transcriber=transcriber))
    first_lookup = SimpleNamespace(result=lambda: None)

    text, direct, lines = pipeline._lyrics(
        PipelineRequest("source.flac", "out", artist="Artist", title="Song"),
        "vocals.flac",
        first_lookup,
    )

    assert text == "right line"
    assert direct == []
    assert lines == found.lines
    pipeline_module.discover_lyrics.assert_called_once_with("Song", "Artist")


def test_pipeline_uses_discovered_timed_lines_instead_of_full_song_alignment(tmp_path, monkeypatch):
    source, output = tmp_path / "source.wav", tmp_path / "out"
    sf.write(source, np.zeros((44100 * 4, 2), dtype=np.float32), 44100)
    monkeypatch.setattr("AI.pipeline.analyze_music", lambda _path: {"bpm": 120, "key": "A minor"})
    timed_lines = (TimedLine(1.0, "right line"), TimedLine(3.0, "next line"))
    monkeypatch.setattr(
        "AI.pipeline.discover_lyrics",
        lambda _title, _artist=None: LyricsDiscovery(
            "right line\nnext line", "LRCLIB", "Artist Song", lines=timed_lines
        ),
    )

    class TimedAligner(Aligner):
        @staticmethod
        def align_timed_lines(_audio, text, lines, _language):
            assert text == "right line\nnext line"
            assert lines == timed_lines
            return [
                Word(1.0, 1.4, "right", index=0),
                Word(1.4, 1.8, "line", index=1),
                Word(3.0, 3.4, "next", index=2),
                Word(3.4, 3.8, "line", index=3),
            ]

        @staticmethod
        def align_long_text(*_args):
            raise AssertionError("full-song alignment must not run")

    engines = SimpleNamespace(
        separator=Separator(), pitch=Pitch(), transcriber=None, aligner=TimedAligner()
    )

    KaraokePipeline(engines=engines).run(PipelineRequest(source, output, title="Artist - Song"))

    payload = __import__("json").loads((output / "lyricsSync.json").read_text(encoding="utf-8"))
    assert [(word["start"], word["end"]) for word in payload["words"]] == [
        (1.0, 1.4),
        (1.4, 1.8),
        (3.0, 3.4),
        (3.4, 3.8),
    ]


def test_lyrics_discovery_skips_pisni_once_the_lookup_budget_is_spent(monkeypatch):
    import AI.lyrics_sources as lyrics_sources

    monkeypatch.setattr(lyrics_sources, "_request", lambda url, encoding="utf-8": "[]")
    # The whole lookup already ran out its budget by the time LRCLIB replied
    # (a slow/hanging network, not a fast failure) -- pisni.org.ua must not
    # be attempted at all rather than adding its own request chain on top.
    clock = iter([0.0, 0.0, lyrics_sources.LOOKUP_BUDGET_SECONDS + 1])
    monkeypatch.setattr(lyrics_sources.time, "monotonic", lambda: next(clock))
    pisni_called = []
    monkeypatch.setattr(lyrics_sources, "_pisni", lambda *args: pisni_called.append(args) or None)

    assert discover_lyrics("Лови момент", "Антитiла") is None
    assert pisni_called == []


def test_pisni_stops_walking_candidate_links_once_the_deadline_passes(monkeypatch):
    import AI.lyrics_sources as lyrics_sources

    search = '<a href="/songs/1.html">A</a><a href="/songs/2.html">B</a>'
    requested = []

    def response(url, encoding="utf-8"):
        del encoding
        requested.append(url)
        return search if "search.php" in url else "<h1></h1>"

    monkeypatch.setattr(lyrics_sources, "_request", response)
    # An already-expired budget must not start even the search request.
    result = lyrics_sources._pisni("Антитіла", "Лови момент", "query", deadline=0.0)

    assert result is None
    assert requested == []


def test_lyrics_request_returns_at_the_global_deadline_when_transport_stalls(monkeypatch):
    import AI.lyrics_sources as lyrics_sources

    release = threading.Event()
    monkeypatch.setattr(
        lyrics_sources,
        "_request",
        lambda *_args: release.wait(1) or "[]",
    )
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="deadline"):
            lyrics_sources._request_before("https://lyrics.test", "utf-8", started + 0.03)
        assert time.monotonic() - started < 0.2
    finally:
        release.set()


def test_source_repeat_notation_is_expanded_without_leaking_markers():
    assert _expand_notation("Лови момент | (3)") == "\n".join(["Лови момент"] * 3)


def test_forced_aligner_does_not_expect_timestamps_for_punctuation(monkeypatch):
    text = "Ты станешь слаще —\nА я"
    items = [
        {"text": token.replace("-", ""), "start_time": index, "end_time": index + 0.5}
        for index, token in enumerate(tokenize(text))
    ]
    model = SimpleNamespace(align=lambda **_kwargs: [SimpleNamespace(items=items)])
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = model
    monkeypatch.setattr("AI.engines.text.duration", lambda _audio: 10)

    words = aligner.align("vocals.flac", text, "ru")

    assert [word.text for word in words] == ["Ты", "станешь", "слаще", "А", "я"]


def test_isolated_forced_aligner_honours_cancellation_before_model_start():
    aligner = Qwen3ForcedAligner("production-model", isolated=True)
    aligner.set_cancelled(lambda: True)

    with pytest.raises(ProcessingCancelledError, match="cancelled"):
        aligner.align_long_text("vocals.flac", "words", "en")


def test_isolated_forced_aligner_terminates_running_worker_on_cancellation(monkeypatch):
    process = Mock()
    process.is_alive.return_value = True
    results = Mock()
    from queue import Empty

    results.get.side_effect = Empty
    context = Mock()
    context.Queue.return_value = results
    context.Process.return_value = process
    monkeypatch.setattr(
        "AI.engines.alignment_process.multiprocessing.get_context",
        Mock(return_value=context),
    )
    monkeypatch.setenv("KARAOKE_AI_ENABLE_HEAVY_ALIGNER", "1")
    aligner = Qwen3ForcedAligner("production-model", isolated=True)
    aligner.set_cancelled(Mock(side_effect=[False, False, True]))

    with pytest.raises(ProcessingCancelledError, match="cancelled"):
        aligner.align_long_text("vocals.flac", "words", "en")

    process.start.assert_called_once_with()
    process.terminate.assert_called_once_with()


def test_forced_alignment_has_a_bounded_default_deadline(monkeypatch):
    from AI.engines.alignment_process import alignment_timeout_seconds

    monkeypatch.delenv("KARAOKE_AI_ALIGN_TIMEOUT_SECONDS", raising=False)
    assert alignment_timeout_seconds() == 60.0
    monkeypatch.setenv("KARAOKE_AI_ALIGN_TIMEOUT_SECONDS", "4")
    assert alignment_timeout_seconds() == 30.0


def test_forced_alignment_deadline_terminates_the_running_worker(monkeypatch):
    from queue import Empty

    from AI.engines.alignment_process import IsolatedAlignmentProcess

    process = Mock()
    process.is_alive.return_value = True
    results = Mock()
    results.get.side_effect = Empty
    context = Mock()
    context.Queue.return_value = results
    context.Process.return_value = process
    monkeypatch.setenv("KARAOKE_AI_ALIGN_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr(
        "AI.engines.alignment_process.multiprocessing.get_context",
        Mock(return_value=context),
    )
    monkeypatch.setattr(
        "AI.engines.alignment_process.time.monotonic",
        Mock(side_effect=[0.0, 31.0]),
    )

    worker = IsolatedAlignmentProcess("production-model")
    with pytest.raises(AlignmentTimeoutError, match="30-second"):
        worker.run("align_long_text", "vocals.flac", "words", "en")

    process.terminate.assert_called_once_with()


def test_alignment_deadline_does_not_publish_uniform_timing_in_strict_mode(monkeypatch):
    aligner = SimpleNamespace(
        align_long_text=Mock(
            side_effect=AlignmentTimeoutError("Forced alignment exceeded the 120-second limit")
        ),
        set_cancelled=Mock(),
    )
    pipeline = KaraokePipeline(
        config=SimpleNamespace(allow_fallback=False),
        engines=SimpleNamespace(aligner=aligner),
    )
    monkeypatch.setattr(
        pipeline_module.UniformTextFallback,
        "align",
        Mock(side_effect=AssertionError("strict mode must not publish fake timing")),
    )

    with pytest.raises(AlignmentTimeoutError):
        pipeline._align("vocals.flac", "hello", "en", [])


def test_cuda_alignment_failure_does_not_publish_uniform_timing_in_strict_mode(monkeypatch):
    aligner = SimpleNamespace(
        align_long_text=Mock(side_effect=EngineUnavailableError("CUDA error: unknown error")),
        set_cancelled=Mock(),
    )
    pipeline = KaraokePipeline(
        config=SimpleNamespace(allow_fallback=False),
        engines=SimpleNamespace(aligner=aligner),
    )
    monkeypatch.setattr(
        pipeline_module.UniformTextFallback,
        "align",
        Mock(side_effect=AssertionError("strict mode must not publish fake timing")),
    )

    with pytest.raises(EngineUnavailableError, match="CUDA error"):
        pipeline._align("vocals.flac", "hello", "en", [])


def test_timed_alignment_failure_is_not_published_as_fake_uniform_timing(monkeypatch):
    aligner = SimpleNamespace(
        name="ctc",
        align_timed_lines=Mock(side_effect=EngineUnavailableError("ctc failed")),
    )
    pipeline = KaraokePipeline(
        config=SimpleNamespace(allow_fallback=False),
        engines=SimpleNamespace(aligner=aligner),
    )
    monkeypatch.setattr(
        pipeline_module.UniformTextFallback,
        "align",
        Mock(side_effect=AssertionError("uniform timing must not be published")),
    )

    with pytest.raises(EngineUnavailableError, match="ctc failed"):
        pipeline._align(
            "vocals.flac",
            "hello",
            "en",
            [],
            (TimedLine(1.0, "hello"),),
        )


def test_unsupported_language_uses_safe_vocal_timing_without_heavy_qwen(monkeypatch, tmp_path):
    audio = tmp_path / "vocals.wav"
    sf.write(audio, np.zeros(44100, dtype=np.float32), 44100)
    model = SimpleNamespace(align=Mock(side_effect=AssertionError("must not start")))
    aligner = Qwen3ForcedAligner("production-model", isolated=False)
    aligner._model = model
    monkeypatch.delenv("KARAOKE_AI_ENABLE_HEAVY_ALIGNER", raising=False)

    words = aligner.align_long_text(audio, "かわいそうかわいそう", "en")

    model.align.assert_not_called()
    assert [word.text for word in words] == ["かわいそうかわいそう"]
    assert 0 <= words[0].start < words[0].end <= 1


def test_failed_ctc_uses_safe_vocal_timing_without_heavy_qwen(monkeypatch, tmp_path, capsys):
    audio = tmp_path / "vocals.wav"
    sf.write(audio, np.zeros(44100, dtype=np.float32), 44100)
    model = SimpleNamespace(align=Mock(side_effect=AssertionError("must not start")))
    aligner = Qwen3ForcedAligner("production-model", isolated=False)
    aligner._model = model
    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ctc-model")
    monkeypatch.delenv("KARAOKE_AI_ENABLE_HEAVY_ALIGNER", raising=False)
    monkeypatch.setattr(aligner, "_ctc_full", Mock(return_value=None))

    words = aligner.align_long_text(audio, "неподдерживаемый текст", "ru")

    model.align.assert_not_called()
    assert [word.text for word in words] == ["неподдерживаемый", "текст"]
    assert "alignment route language=Russian" in capsys.readouterr().out


def test_coarse_line_starts_follow_vocal_time_across_an_instrumental_gap():
    starts = coarse_line_starts(
        [4, 4, 4],
        [(10.0, 20.0), (40.0, 50.0)],
        span=60.0,
    )

    assert starts == pytest.approx([10.0, 16.667, 43.333], abs=0.001)


def test_long_plain_lyrics_use_bounded_ctc_windows_before_full_song_ctc(
    monkeypatch,
):
    aligner = Qwen3ForcedAligner("production-model", isolated=False)
    expected = [Word(10.0, 10.5, "первая", 0.9, 0)]
    coarse = Mock(return_value=expected)
    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ctlich-model")
    monkeypatch.setattr("AI.engines.text.duration", lambda _audio: 180.0)
    monkeypatch.setattr(
        "AI.audio.read_mono", lambda _audio: (np.zeros(10, dtype=np.float32), 44_100)
    )
    monkeypatch.setattr(aligner, "_ctc_coarse_text", coarse)
    monkeypatch.setattr(
        aligner,
        "_ctc_full",
        Mock(side_effect=AssertionError("full-song tensor must not be allocated")),
    )

    result = aligner._align_long_text_local("vocals.flac", "первая\nстрока", "Russian")

    assert result == expected
    coarse.assert_called_once()


def test_unsupported_language_is_aligned_without_spawning_worker(monkeypatch, tmp_path):
    audio = tmp_path / "vocals.wav"
    sf.write(audio, np.zeros(44100, dtype=np.float32), 44100)
    aligner = Qwen3ForcedAligner("production-model", isolated=True)
    monkeypatch.delenv("KARAOKE_AI_ENABLE_HEAVY_ALIGNER", raising=False)
    monkeypatch.setattr(
        aligner,
        "_run_isolated",
        Mock(side_effect=AssertionError("worker must not spawn")),
    )

    words = aligner.align_long_text(audio, "かわいそうかわいそう", "en")

    aligner._run_isolated.assert_not_called()
    assert [word.text for word in words] == ["かわいそうかわいそう"]


def test_alignment_worker_caps_cpu_threads_and_cuda_memory(monkeypatch):
    from AI.engines.alignment_process import configure_worker_resource_limits

    cuda = SimpleNamespace(set_per_process_memory_fraction=Mock())
    torch = SimpleNamespace(cuda=cuda, set_num_threads=Mock())
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.delenv("KARAOKE_AI_ALIGN_GPU_MEMORY_FRACTION", raising=False)

    configure_worker_resource_limits("cuda")

    torch.set_num_threads.assert_called_once_with(2)
    cuda.set_per_process_memory_fraction.assert_called_once_with(0.5)
    assert os.environ["OMP_NUM_THREADS"] == "2"


def test_processing_stage_percentages_reserve_progress_for_post_alignment_work():
    from app.services.pipeline_service import _AI_STAGE_PLAN

    percentages = [_AI_STAGE_PLAN[name][0] for name in ("align", "notes", "validate", "complete")]
    assert percentages == sorted(set(percentages))
    assert percentages[0] <= 94.0


def test_forced_aligner_merges_japanese_model_words_into_canonical_lyric_token(
    monkeypatch,
):
    text = "かわいそうかわいそう katana"
    items = [
        {"text": "かわいそう", "start_time": 0.5, "end_time": 1.0},
        {"text": "かわいそう", "start_time": 1.0, "end_time": 1.6},
        {"text": "katana", "start_time": 2.0, "end_time": 2.8},
    ]
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=lambda **_kwargs: [SimpleNamespace(items=items)])
    monkeypatch.setattr("AI.engines.text.duration", lambda _audio: 4.0)

    words = aligner.align("vocals.flac", text, "en")

    assert [word.text for word in words] == ["かわいそうかわいそう", "katana"]
    assert [(word.start, word.end) for word in words] == [(0.5, 1.6), (2.0, 2.8)]


def test_long_asr_is_batched_by_vocal_chunks(monkeypatch):
    from AI.engines.text import Qwen3Transcriber

    calls = []
    model = SimpleNamespace(
        transcribe=lambda **kwargs: (
            calls.append(kwargs) or [SimpleNamespace(text="первая"), SimpleNamespace(text="вторая")]
        )
    )
    transcriber = Qwen3Transcriber("test-model")
    transcriber._model = model
    chunks = [(np.ones(16000, dtype=np.float32), 16000)] * 2
    monkeypatch.setattr("AI.engines.text._asr_voice_chunks", lambda _audio: chunks)

    text, words = transcriber.transcribe("vocals.flac", "ru")

    assert text == "первая\nвторая"
    assert words == []
    assert calls[0]["audio"] == chunks
    assert calls[0]["language"] == ["Russian", "Russian"]


def test_asr_chunks_cover_the_entire_song_without_vad_gaps(monkeypatch):
    from AI.engines.text import _asr_voice_chunks

    samples = np.ones(1_000, dtype=np.float32)
    monkeypatch.setattr("AI.audio.read_mono", lambda _audio: (samples, 10))
    monkeypatch.setattr(
        "AI.word_voicing.voice_activity_intervals",
        lambda _audio: [(5.0, 6.0), (40.0, 41.0), (80.0, 81.0)],
    )

    chunks = _asr_voice_chunks("vocals.flac", max_seconds=30.0)

    assert chunks.windows[0][0] == 0.0
    assert chunks.windows[-1][1] == 100.0
    assert all(
        previous[1] == current[0]
        for previous, current in zip(chunks.windows, chunks.windows[1:], strict=False)
    )
    assert all(end - start <= 30.0 for start, end in chunks.windows)
    assert sum(len(audio) for audio, _rate in chunks) == len(samples)
    assert chunks.line_starts == (5.0, 40.0, 80.0, 90.0)


def test_neural_asr_exposes_real_audio_chunk_starts_for_forced_alignment(monkeypatch):
    from AI.engines.text import Qwen3Transcriber

    class TimedChunks(list):
        windows = ((5.0, 12.0), (15.0, 22.0))

    chunks = TimedChunks([
        (np.ones(16000, dtype=np.float32), 16000),
        (np.ones(16000, dtype=np.float32), 16000),
    ])
    model = SimpleNamespace(
        transcribe=lambda **_kwargs: [
            SimpleNamespace(text="первая строка"),
            SimpleNamespace(text="вторая строка"),
        ]
    )
    transcriber = Qwen3Transcriber("test-model")
    transcriber._model = model
    monkeypatch.setattr("AI.engines.text._asr_voice_chunks", lambda _audio: chunks)

    text, words = transcriber.transcribe_neural("vocals.flac", "ru")

    assert text == "первая строка\nвторая строка"
    assert words == []
    assert transcriber.last_timed_lines == (
        (5.0, "первая строка"),
        (15.0, "вторая строка"),
    )


def test_asr_context_is_broadcast_and_exact_echo_lines_are_removed(monkeypatch):
    from AI.engines.text import Qwen3Transcriber

    calls = []
    model = SimpleNamespace(
        transcribe=lambda **kwargs: (
            calls.append(kwargs)
            or [SimpleNamespace(text="Исполнитель — Песня.\nНастоящие слова\nИсполнитель — Песня!")]
        )
    )
    transcriber = Qwen3Transcriber("test-model")
    transcriber._model = model
    chunk = (np.ones(16000, dtype=np.float32), 16000)
    monkeypatch.setattr("AI.engines.text._asr_voice_chunks", lambda _audio: [chunk])

    text, words = transcriber.transcribe_neural(
        "vocals.flac",
        "ru",
        context="Исполнитель — Песня",
    )

    assert calls[0]["context"] == "Исполнитель — Песня"
    assert text == "Настоящие слова"
    assert words == []


def test_unknown_song_prefers_complete_neural_lyrics_over_raw_ctc_transcript(monkeypatch):
    from AI.engines.text import Qwen3Transcriber

    ctc_words = [
        Word(float(index), float(index) + 0.5, token, 0.8, index)
        for index, token in enumerate(("сырай", "текст", "распознан", "не", "очень"))
    ]
    neural_text = "Связный текст песни распознан намного точнее полностью"
    transcriber = Qwen3Transcriber("test-model")
    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ctc-model")
    monkeypatch.setattr(
        "AI.engines.text._create_ctc_aligner",
        lambda _path, _language: SimpleNamespace(transcribe=Mock(return_value=ctc_words)),
    )
    monkeypatch.setattr(
        transcriber,
        "transcribe_neural",
        Mock(return_value=(neural_text, [])),
    )

    text, words = transcriber.transcribe("vocals.flac", "ru")

    assert text == neural_text
    assert words == []


def test_complete_neural_transcript_skips_redundant_raw_ctc_pass(monkeypatch):
    from AI.engines.text import Qwen3Transcriber

    transcriber = Qwen3Transcriber("test-model")
    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ctc-model")
    monkeypatch.setattr(
        "AI.engines.text._create_ctc_aligner",
        lambda _path, _language: SimpleNamespace(
            transcribe=Mock(side_effect=AssertionError("raw CTC must remain lazy"))
        ),
    )
    monkeypatch.setattr(
        transcriber,
        "transcribe_neural",
        Mock(return_value=("Полный связный текст неизвестной песни", [])),
    )

    text, words = transcriber.transcribe("vocals.flac", "ru")

    assert text == "Полный связный текст неизвестной песни"
    assert words == []


def test_russian_asr_falls_back_to_ctc_when_qwen_is_unavailable(monkeypatch):
    from AI.engines.ctc import CTCWordAligner
    from AI.engines.text import Qwen3Transcriber

    direct = [
        Word(index, index + 0.5, token, 0.7, index)
        for index, token in enumerate(("это", "быстрый", "прямой", "текст"))
    ]
    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ctc-model")
    monkeypatch.setattr(CTCWordAligner, "transcribe", lambda _self, _audio: direct)
    transcriber = Qwen3Transcriber("qwen-model")
    monkeypatch.setattr(
        transcriber,
        "transcribe_neural",
        Mock(side_effect=EngineUnavailableError("Qwen unavailable")),
    )

    text, words = transcriber.transcribe("vocals.flac", "ru")

    assert text == "это быстрый прямой текст"
    assert words == direct
    assert transcriber._model is None


def test_ctc_candidate_alignment_scores_all_texts_in_one_engine_call(monkeypatch):
    calls = []

    class AcousticCTC:
        @staticmethod
        def align_transcripts(audio, transcripts, *, split_seconds=None):
            assert split_seconds is None
            calls.append((audio, transcripts))
            return [
                (-1.1, [Word(0.0, 0.2, "первая", 0.7, 0)]),
                (-0.6, [Word(0.0, 0.2, "вторая", 0.8, 0)]),
            ]

    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ctc-model")
    monkeypatch.setattr(
        "AI.engines.text._create_ctc_aligner",
        lambda _model, _language: AcousticCTC(),
    )
    aligner = Qwen3ForcedAligner("unused", isolated=False)

    result = aligner.align_ctc_candidates("vocals.flac", ["Первая", "Вторая"], "ru")

    assert [item[0] for item in result] == [-1.1, -0.6]
    assert calls == [("vocals.flac", [["первая"], ["вторая"]])]


def test_ctc_transcript_candidates_share_one_neural_forward_pass():
    from AI.engines.ctc import CTCWordAligner

    aligner = object.__new__(CTCWordAligner)
    aligner._acoustic_logits = Mock(return_value=("logits", "model", "processor", 12.0))
    aligner._align_logits = Mock(
        side_effect=[
            (-1.0, [Word(0.0, 0.2, "one", 0.7, 0)]),
            (-0.5, [Word(0.0, 0.2, "two", 0.8, 0)]),
        ]
    )

    result = aligner.align_transcripts("vocals.flac", [["one"], ["two"]])

    assert [item[0] for item in result] == [-1.0, -0.5]
    aligner._acoustic_logits.assert_called_once_with("vocals.flac")
    assert aligner._align_logits.call_count == 2


def test_ctc_candidate_alignment_splits_the_common_prefix_from_changed_tail():
    from AI.engines.ctc import CTCWordAligner

    aligner = object.__new__(CTCWordAligner)
    acoustic = ("logits", "model", "processor", 12.0)
    expected = [(-0.5, [Word(0.0, 0.2, "one", 0.8, 0)])]
    aligner._acoustic_logits = Mock(return_value=acoustic)
    aligner._align_split_transcripts = Mock(return_value=expected)

    result = aligner.align_transcripts("vocals.flac", [["one", "tail"]], split_seconds=7.0)

    assert result == expected
    aligner._acoustic_logits.assert_called_once_with("vocals.flac")
    aligner._align_split_transcripts.assert_called_once_with(*acoustic, [["one", "tail"]], 7.0)


def test_long_alignment_realigns_collapsed_ranges_acoustically(tmp_path, monkeypatch):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 5, dtype=np.float32), 44100)
    collapsed = [
        {"text": token, "start_time": start, "end_time": end}
        for token, start, end in [
            ("one", 0.2, 0.8),
            ("two", 1, 1),
            ("three", 1, 1),
            ("four", 3, 3.6),
        ]
    ]
    repaired = [
        {"text": token, "start_time": index * 0.8, "end_time": index * 0.8 + 0.4}
        for index, token in enumerate(("one", "two", "three", "four"))
    ]

    def align(audio, **_kwargs):
        return [SimpleNamespace(items=repaired if isinstance(audio, tuple) else collapsed)]

    model = SimpleNamespace(
        align=lambda audio, **kwargs: (
            [SimpleNamespace(items=repaired)] if isinstance(audio, list) else align(audio, **kwargs)
        )
    )
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = model

    words = aligner.align_long_text(audio, "one two three four", "en")

    assert [(round(word.start, 3), round(word.end, 3)) for word in words] == [
        (0.2, 0.8),
        (0.8, 1.2),
        (1.6, 2),
        (3, 3.6),
    ]


def test_collapsed_ranges_are_repaired_locally_without_losing_acoustic_onset(tmp_path):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 20, dtype=np.float32), 44100)
    initial = [
        ("first", 12.9, 13.7),
        ("short", 13.7, 13.7),
        ("phrase", 13.7, 13.7),
        ("anchor", 15.6, 16.2),
    ]
    repaired = [
        ("first", 1.0, 1.8),
        ("short", 1.8, 2.1),
        ("phrase", 2.1, 2.8),
        ("anchor", 3.7, 4.3),
    ]

    def align(audio, **_kwargs):
        rows = repaired if isinstance(audio, list) else initial
        return (
            [
                SimpleNamespace(
                    items=[
                        {"text": token, "start_time": start, "end_time": end}
                        for token, start, end in rows
                    ]
                )
                for _ in audio
            ]
            if isinstance(audio, list)
            else SimpleNamespace(
                items=[
                    {"text": token, "start_time": start, "end_time": end}
                    for token, start, end in rows
                ]
            )
        )

    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=align)

    words = aligner.align_long_text(audio, "first short phrase anchor", "en")

    assert words[0].start == 12.9
    assert [(round(word.start, 3), round(word.end, 3)) for word in words[1:3]] == [
        (13.7, 14.0),
        (14.0, 14.7),
    ]


def test_invalid_runs_do_not_merge_across_valid_acoustic_anchors():
    words = [
        Word(1, 2, "one"),
        Word(2, 2, "bad"),
        Word(3, 4, "anchor"),
        Word(4, 4, "bad"),
    ]

    assert _invalid_runs(words, 5) == [(1, 2), (3, 4)]


def test_timed_line_boundary_disagreement_is_clamped_monotonically():
    words = [Word(1.8, 2.0, "first"), Word(1.7, 2.2, "second")]

    _enforce_monotonic_starts(words, 3.0)

    assert (words[1].start, words[1].end) == (1.8, 2.2)


def test_collapsed_multiword_line_expands_inside_its_trusted_lrc_window():
    words = [
        Word(10.0, 10.1, "one", index=0),
        Word(10.1, 10.2, "longer", index=1),
        Word(10.2, 10.3, "three", index=2),
        Word(15.0, 15.5, "next", index=3),
    ]

    _repair_collapsed_timed_lines(words, [(10.0, 0, 3), (15.0, 3, 4)], 20.0)

    assert words[0].start == 10.0
    assert words[2].end == 15.0
    assert all(words[index].end <= words[index + 1].start for index in range(2))
    assert words[3] == Word(15.0, 15.5, "next", index=3)


def test_unresolved_timed_word_repairs_only_its_lrc_line():
    words = [Word(1.1, 1.4, "first", index=0), None, Word(5.1, 5.5, "kept", index=2)]
    entries = [(1.0, 0, 2), (5.0, 2, 3)]

    _fill_unresolved_timed_lines(words, entries, ["first", "missing", "kept"], 8.0)

    assert words[0] == Word(1.1, 1.4, "first", index=0)
    assert words[1] == Word(1.4, 5.0, "missing", 0.0, 1)
    assert words[2] == Word(5.1, 5.5, "kept", index=2)


def test_acoustic_runs_find_words_crossing_silence_and_overlapping_neighbors():
    rate = 100
    samples = np.ones(rate * 5, dtype=np.float32)
    samples[rate * 2 : rate * 3] = 0
    words = [
        Word(0.2, 0.8, "clear"),
        Word(1.8, 3.2, "silence"),
        Word(3.3, 4.2, "overlap"),
        Word(4.0, 4.7, "neighbor"),
    ]

    assert _acoustic_runs(words, samples, rate) == [(1, 4)]


def test_ordinary_song_length_uses_a_single_aligner_call_not_windowing(tmp_path):
    # A typical 6-minute song must never hit the windowing path: every
    # attempt to patch window-seam coverage just moved the artifact to the
    # next boundary instead of removing it, so ordinary songs now get one
    # consistent aligner call covering the whole thing instead of being
    # stitched together from several overlapping windows.
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(3600, dtype=np.float32), 10)  # 360s span
    tokens = [f"word{index}" for index in range(10)]

    def align(audio, text, **_kwargs):
        assert audio == str(audio_path)
        assert text == " ".join(tokens)
        return [
            {"text": token, "start_time": index + 0.5, "end_time": index + 0.9}
            for index, token in enumerate(tokens)
        ]

    audio_path = audio
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=align)

    words = aligner.align_long_text(audio, " ".join(tokens), "en")

    assert len(words) == len(tokens)
    assert words[0].start == 0.5


def test_only_genuine_outliers_beyond_the_threshold_use_windowing(tmp_path):
    audio = tmp_path / "vocals.flac"
    span_seconds = 610.0  # just over WINDOWED_ALIGNMENT_THRESHOLD_SECONDS (600)
    sf.write(audio, np.zeros(int(span_seconds * 10), dtype=np.float32), 10)
    tokens = [f"word{index}" for index in range(36)]
    calls = []

    def align(audio, text, **_kwargs):
        calls.append((audio, text))
        return [
            SimpleNamespace(
                items=[
                    {
                        "text": token,
                        "start_time": index * 5 + 0.1,
                        "end_time": index * 5 + 0.5,
                    }
                    for index, token in enumerate(line.split())
                ]
            )
            for line in text
        ]

    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=align)
    aligner.align_long_text(audio, " ".join(tokens), "en")

    assert len(calls) == 1
    segments = [len(segment) for segment, _rate in calls[0][0]]
    # Every main window overlaps its neighbour by >=50% (900 samples each),
    # plus a dedicated intro and outro window (450 samples each) appended last.
    assert segments[-2:] == [450, 450]
    assert all(length == 900 for length in segments[:-2])
    assert len(segments) > 2


def test_separation_cancel_stops_worker_immediately():
    class Process:
        alive = True
        exitcode = None

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            del timeout

        def terminate(self):
            self.alive = False

    class Queue:
        def put(self, _value):
            pass

        def put_nowait(self, _value):
            pass

        def close(self):
            pass

        def cancel_join_thread(self):
            pass

    separator = MSSTMelRoformerSeparator()
    process = Process()
    separator._process, separator._requests, separator._results = process, Queue(), Queue()

    with pytest.raises(ProcessingCancelledError):
        separator._run("input", "output", {}, cancelled=lambda: True)

    assert process.alive is False


def test_long_ukrainian_alignment_uses_acoustic_ctc_when_qwen_remains_invalid(
    tmp_path, monkeypatch
):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 4, dtype=np.float32), 44100)
    tokens = ["Заглядай", "у", "очі"]
    model = SimpleNamespace(
        align=lambda **_kwargs: [
            SimpleNamespace(
                items=[{"text": token, "start_time": 0, "end_time": 0} for token in tokens]
            )
        ]
    )
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = model

    class AcousticCTC:
        def __init__(self, model_path):
            assert model_path == "uk-model"

        def align(self, _samples, _rate, canonical, offset):
            # The acoustic CTC model only ever sees a normalized (casefolded,
            # letters-only) transcript — never the canonical mixed-case lyric
            # text — so it can encode every token against its vocabulary.
            assert canonical == [token.casefold() for token in tokens]
            assert offset == 0
            return [
                Word(index + 0.2, index + 0.8, token, 0.9, index)
                for index, token in enumerate(canonical)
            ]

    monkeypatch.setenv("KARAOKE_AI_CTC_UK_MODEL", "uk-model")
    monkeypatch.setattr("AI.engines.ctc.CTCWordAligner", AcousticCTC)

    words = aligner.align_long_text(audio, "Заглядай у очі", "ru")

    assert [(word.text, word.start, word.end) for word in words] == [
        ("Заглядай", 0.2, 0.8),
        ("у", 1.2, 1.8),
        ("очі", 2.2, 2.8),
    ]
    assert aligner.needs_voice_anchoring is False


@pytest.mark.parametrize(
    ("canonical", "timed"),
    [
        ("one two", "one three"),
        ("один два", "один"),
        ("hello", "hello extra"),
        ("don't stop", "do stop"),
    ],
)
def test_timed_line_plan_rejects_non_equivalent_lyrics(canonical, timed):
    with pytest.raises(InvalidArtifactError, match="do not match"):
        _timed_line_plan(canonical, (TimedLine(0, timed),), 10.0)


@pytest.mark.parametrize(
    ("canonical", "timed"),
    [
        ("Hello, WORLD!", "hello world"),
        ("Її пісня", "ЇЇ ПІСНЯ"),
        ("don't stop", "DON'T STOP"),
    ],
)
def test_timed_line_plan_normalization_preserves_equivalent_lyrics(canonical, timed):
    tokens, entries, groups = _timed_line_plan(canonical, (TimedLine(0, timed),), 10.0)
    assert len(tokens) == entries[0][2]
    assert groups == [(0, len(tokens), 0, 10.0)]


def test_timed_line_offset_finds_a_different_recording_intro_without_song_rules():
    rate, span, offset = 100, 90.0, 47.5
    samples = np.zeros(round(rate * span), dtype=np.float32)
    lines = (
        TimedLine(3.0, "first line"),
        TimedLine(7.0, "second longer line"),
        TimedLine(12.0, "third line"),
    )
    for line in lines:
        start = round((line.start + offset) * rate)
        samples[start : start + round(1.2 * rate)] = 0.8

    detected = _timed_line_offset(samples, rate, lines, span)

    assert detected == pytest.approx(offset, abs=0.2)


def test_timed_line_retry_policy_covers_every_unresolved_position():
    for count in range(1, 9):
        entries = [(0.0, 0, count)]
        for missing in range(count):
            words = [Word(index, index + 0.5, str(index), 1.0, index) for index in range(count)]
            words[missing] = None
            stages = list(_timed_line_retry_stages(words, entries, float(count + 1)))
            assert any(lower <= missing < upper for stage in stages for lower, upper, *_ in stage)


def test_timed_russian_lines_use_bounded_ctc_windows_before_qwen(tmp_path, monkeypatch):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 4, dtype=np.float32), 44100)
    tokens = ["Этот", "парень", "пел"]
    qwen_calls = []
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=lambda **kwargs: qwen_calls.append(kwargs))

    class AcousticCTC:
        def __init__(self, model_path):
            assert model_path == "ru-model"

        def align(self, _samples, _rate, canonical, offset):
            assert len(_samples) <= _rate * 18
            assert canonical == ["этот", "парень", "пел"]
            local_starts = [0.2, 1.0, 2.2]
            return [
                Word(offset + start, offset + start + 0.25, token, 0.9, index)
                for index, (token, start) in enumerate(zip(canonical, local_starts, strict=True))
            ]

    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ru-model")
    monkeypatch.setattr("AI.engines.ctc.CTCWordAligner", AcousticCTC)

    words = aligner.align_timed_lines(
        audio,
        "Этот парень\nпел",
        (TimedLine(0, "Этот парень"), TimedLine(2, "пел")),
        "ru",
    )

    assert [word.text for word in words] == tokens
    assert [word.start for word in words] == sorted(word.start for word in words)
    assert qwen_calls == []
    assert aligner.needs_voice_anchoring is False


def test_timed_unsupported_language_uses_provider_windows_without_qwen(tmp_path, monkeypatch):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 4, dtype=np.float32), 44100)
    model = SimpleNamespace(align=Mock(side_effect=AssertionError("must not start")))
    aligner = Qwen3ForcedAligner("production-model", isolated=False)
    aligner._model = model
    monkeypatch.delenv("KARAOKE_AI_ENABLE_HEAVY_ALIGNER", raising=False)

    words = aligner.align_timed_lines(
        audio,
        "first line\nsecond line",
        (TimedLine(0.5, "first line"), TimedLine(2.5, "second line")),
        "en",
    )

    model.align.assert_not_called()
    assert [word.text for word in words] == ["first", "line", "second", "line"]
    assert 0.5 <= words[0].start < words[1].end <= 2.5
    assert 2.5 <= words[2].start < words[3].end <= 4.0


def test_timed_russian_ctc_batches_adjacent_lines_into_bounded_windows(tmp_path, monkeypatch):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 12, dtype=np.float32), 44100)
    calls = []
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=Mock(side_effect=AssertionError("Qwen must not load")))

    class AcousticCTC:
        def __init__(self, _model_path):
            pass

        def align(self, samples, rate, canonical, offset):
            calls.append((len(samples) / rate, tuple(canonical), offset))
            return [
                Word(
                    offset + 0.2 + index * 0.25,
                    offset + 0.35 + index * 0.25,
                    token,
                    0.9,
                    index,
                )
                for index, token in enumerate(canonical)
            ]

    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ru-model")
    monkeypatch.setattr("AI.engines.ctc.CTCWordAligner", AcousticCTC)
    lines = tuple(TimedLine(index * 2.0, f"слово{index}") for index in range(6))

    words = aligner.align_timed_lines(
        audio,
        "\n".join(line.text for line in lines),
        lines,
        "ru",
    )

    assert len(words) == 6
    assert len(calls) <= 2
    assert all(window_seconds <= 10.8 for window_seconds, _tokens, _offset in calls)


def test_timed_russian_ctc_does_not_batch_adjacent_identical_lines(tmp_path, monkeypatch):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 8, dtype=np.float32), 44100)
    calls = []
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=Mock(side_effect=AssertionError("Qwen must not load")))

    class AcousticCTC:
        def __init__(self, _model_path):
            pass

        def align(self, samples, rate, canonical, offset):
            calls.append((len(samples) / rate, tuple(canonical), offset))
            return [
                Word(offset + 0.2 + index * 0.25, offset + 0.4 + index * 0.25, token, 0.9, index)
                for index, token in enumerate(canonical)
            ]

    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ru-model")
    monkeypatch.setattr("AI.engines.ctc.CTCWordAligner", AcousticCTC)
    lines = (TimedLine(1.0, "Выхода нет"), TimedLine(4.0, "Выхода нет"))

    words = aligner.align_timed_lines(
        audio,
        "Выхода нет\nВыхода нет",
        lines,
        "ru",
    )

    assert len(words) == 4
    assert [canonical for _seconds, canonical, _offset in calls] == [
        ("выхода", "нет"),
        ("выхода", "нет"),
    ]


def test_timed_russian_ctc_interpolates_only_the_foreign_token(tmp_path, monkeypatch):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 4, dtype=np.float32), 44100)
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=Mock(side_effect=AssertionError("Qwen must not load")))

    class AcousticCTC:
        def __init__(self, _model_path):
            pass

        def align(self, _samples, _rate, canonical, offset):
            assert canonical == ["это"]
            return [Word(offset + 0.2, offset + 0.6, "это", 0.9, 0)]

    monkeypatch.setenv("KARAOKE_AI_CTC_RU_MODEL", "ru-model")
    monkeypatch.setattr("AI.engines.ctc.CTCWordAligner", AcousticCTC)

    words = aligner.align_timed_lines(
        audio,
        "Это\npropellerheads",
        (TimedLine(0, "Это"), TimedLine(2, "propellerheads")),
        "ru",
    )

    assert [word.text for word in words] == ["Это", "propellerheads"]
    assert 2.0 <= words[1].start < words[1].end <= 4.0


def test_timed_lines_only_define_acoustic_windows(tmp_path):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 4, dtype=np.float32), 44100)
    items = [
        {"text": token, "start_time": index, "end_time": index + 0.5}
        for index, token in enumerate(("one", "two", "three"))
    ]
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(align=lambda **_kwargs: [SimpleNamespace(items=items)])

    words = aligner.align_timed_lines(
        audio, "one two\nthree", (TimedLine(0, "one two"), TimedLine(2, "three")), "en"
    )

    assert [word.text for word in words] == ["one", "two", "three"]


def test_isolated_collapsed_word_uses_remaining_interval_between_acoustic_anchors(tmp_path):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 4, dtype=np.float32), 44100)
    items = [
        {"text": "one", "start_time": 0.2, "end_time": 0.8},
        {"text": "two", "start_time": 1, "end_time": 1},
        {"text": "three", "start_time": 1.4, "end_time": 2},
    ]
    aligner = Qwen3ForcedAligner("test-model")
    aligner._model = SimpleNamespace(
        align=lambda audio, **_kwargs: [SimpleNamespace(items=items) for _ in audio]
    )

    words = aligner.align_timed_lines(
        audio, "one two three", (TimedLine(0, "one two three"),), "en"
    )

    assert (words[1].start, words[1].end) == (0.8, 1.4)


def test_collapsed_consonant_preposition_uses_one_model_time_quantum(tmp_path, monkeypatch):
    audio = tmp_path / "vocals.flac"
    sf.write(audio, np.zeros(44100 * 3, dtype=np.float32), 44100)
    items = [
        {"text": "sing", "start_time": 0.2, "end_time": 1},
        {"text": "в", "start_time": 1, "end_time": 1},
        {"text": "городе", "start_time": 1, "end_time": 2},
    ]
    aligner = Qwen3ForcedAligner("test-model")
    monkeypatch.delenv("KARAOKE_AI_CTC_RU_MODEL", raising=False)
    aligner._model = SimpleNamespace(
        timestamp_segment_time=80,
        align=lambda audio, **_kwargs: [SimpleNamespace(items=items) for _ in audio],
    )

    words = aligner.align_timed_lines(
        audio, "sing в городе", (TimedLine(0, "sing в городе"),), "ru"
    )

    assert (round(words[1].start, 2), round(words[1].end, 2), round(words[2].start, 2)) == (
        1,
        1.08,
        1.08,
    )


def test_msst_separator_accepts_flac_vocal_output(tmp_path, monkeypatch):
    engine = tmp_path / "msst"
    engine.mkdir()
    (engine / "inference.py").write_text("# stub", encoding="utf-8")
    config = tmp_path / "config.yaml"
    checkpoint = tmp_path / "model.ckpt"
    config.write_text("stub", encoding="utf-8")
    checkpoint.write_bytes(b"stub")

    mix = tmp_path / "mix.wav"
    vocals = tmp_path / "vocals.flac"
    instrumental = tmp_path / "instrumental.flac"
    audio = np.linspace(-0.2, 0.2, 4410, dtype=np.float32)
    stereo = np.column_stack((audio, audio))
    sf.write(mix, stereo, 44100)

    separator = MSSTMelRoformerSeparator(engine_dir=engine, config=config, checkpoint=checkpoint)

    def fake_run(_source, output, _tuning, **_kwargs):
        stem_dir = output / "song"
        stem_dir.mkdir(parents=True)
        sf.write(stem_dir / "vocals.flac", stereo * 0.75, 44100, subtype="PCM_24")

    monkeypatch.setattr(separator, "_run", fake_run)
    separator.separate(mix, vocals, instrumental)

    vocal_audio, rate = sf.read(vocals, dtype="float32", always_2d=True)
    backing_audio, backing_rate = sf.read(instrumental, dtype="float32", always_2d=True)
    assert rate == backing_rate == 44100
    assert vocal_audio.shape == backing_audio.shape == stereo.shape
    assert np.max(np.abs(vocal_audio)) > 0.1
    assert np.max(np.abs((vocal_audio + backing_audio) - stereo)) < 2e-4


def test_msst_separator_stages_input_outside_glob_metacharacter_song_directory(
    tmp_path, monkeypatch
):
    engine = tmp_path / "msst"
    engine.mkdir()
    (engine / "inference.py").write_text("# stub", encoding="utf-8")
    config = tmp_path / "config.yaml"
    checkpoint = tmp_path / "model.ckpt"
    config.write_text("stub", encoding="utf-8")
    checkpoint.write_bytes(b"stub")

    song_dir = tmp_path / "O.Torvald [ ] Radio"
    song_dir.mkdir()
    mix = song_dir / "mix.wav"
    vocals = song_dir / "vocals.flac"
    instrumental = song_dir / "instrumental.flac"
    stereo = np.zeros((4410, 2), dtype=np.float32)
    sf.write(mix, stereo, 44100)

    separator = MSSTMelRoformerSeparator(engine_dir=engine, config=config, checkpoint=checkpoint)

    def fake_run(source, output, _tuning, **_kwargs):
        # Reproduce the exact enumeration used by the vendored MSST engine.
        import glob
        import os

        inputs = glob.glob(os.path.join(str(source), "**/*.*"), recursive=True)
        assert [os.path.basename(item) for item in inputs] == ["song.wav"]
        assert "[" not in str(source)
        stem_dir = output / "song"
        stem_dir.mkdir(parents=True)
        sf.write(stem_dir / "vocals.wav", stereo, 44100)

    monkeypatch.setattr(separator, "_run", fake_run)
    separator.separate(mix, vocals, instrumental)

    assert vocals.is_file()
    assert instrumental.is_file()


def test_msst_separator_uses_the_backing_stem_directly_without_rereading_the_full_mix(
    tmp_path, monkeypatch
):
    # When MSST produces its own no_vocal/instrumental stem, the instrumental
    # output must come straight from that stem (not from a recomputed
    # mix - vocal, which would need the full mix decoded again). A backing
    # signal deliberately different from mix - vocal makes the two paths
    # distinguishable: matching it only happens if the stem itself was used.
    engine = tmp_path / "msst"
    engine.mkdir()
    (engine / "inference.py").write_text("# stub", encoding="utf-8")
    config = tmp_path / "config.yaml"
    checkpoint = tmp_path / "model.ckpt"
    config.write_text("stub", encoding="utf-8")
    checkpoint.write_bytes(b"stub")

    mix = tmp_path / "mix.wav"
    vocals = tmp_path / "vocals.flac"
    instrumental = tmp_path / "instrumental.flac"
    audio = np.linspace(-0.2, 0.2, 4410, dtype=np.float32)
    stereo = np.column_stack((audio, audio))
    sf.write(mix, stereo, 44100)
    vocal_stem = stereo * 0.75
    backing_stem = stereo * 0.1

    separator = MSSTMelRoformerSeparator(engine_dir=engine, config=config, checkpoint=checkpoint)

    def fake_run(_source, output, _tuning, **_kwargs):
        stem_dir = output / "song"
        stem_dir.mkdir(parents=True)
        sf.write(stem_dir / "vocals.flac", vocal_stem, 44100, subtype="PCM_24")
        sf.write(stem_dir / "no_vocal.flac", backing_stem, 44100, subtype="PCM_24")

    monkeypatch.setattr(separator, "_run", fake_run)

    real_read = sf.read
    read_calls = []

    def counting_read(path, *args, **kwargs):
        read_calls.append(str(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(sf, "read", counting_read)
    separator.separate(mix, vocals, instrumental)

    backing_audio, backing_rate = sf.read(instrumental, dtype="float32", always_2d=True)
    assert backing_rate == 44100
    assert np.max(np.abs(backing_audio - backing_stem)) < 2e-4
    assert str(mix) not in read_calls


def test_msst_separator_reports_actual_audio_outputs_when_vocal_is_missing(tmp_path, monkeypatch):
    engine = tmp_path / "msst"
    engine.mkdir()
    (engine / "inference.py").write_text("# stub", encoding="utf-8")
    config = tmp_path / "config.yaml"
    checkpoint = tmp_path / "model.ckpt"
    config.write_text("stub", encoding="utf-8")
    checkpoint.write_bytes(b"stub")

    mix = tmp_path / "mix.wav"
    sf.write(mix, np.zeros((1000, 2), dtype=np.float32), 44100)
    separator = MSSTMelRoformerSeparator(engine_dir=engine, config=config, checkpoint=checkpoint)

    def fake_run(_source, output, _tuning, **_kwargs):
        stem_dir = output / "song"
        stem_dir.mkdir(parents=True)
        sf.write(stem_dir / "other.flac", np.zeros((1000, 2), dtype=np.float32), 44100)

    monkeypatch.setattr(separator, "_run", fake_run)
    with pytest.raises(Exception, match=r"audio outputs: song[/\\]other\.flac"):
        separator.separate(mix, tmp_path / "vocals.flac", tmp_path / "instrumental.flac")
