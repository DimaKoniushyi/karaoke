from __future__ import annotations

import json
import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import soundfile as sf

from .artifacts import publish_files_atomically
from .audio import decode_audio, duration
from .audio_metadata_v2 import (
    download_cover,
    normalize_local_cover,
    resolve_audio_metadata,
)
from .config import CoreConfig
from .engines.device import release_torch_memory
from .engines.registry import EngineRegistry
from .engines.singing_score import (
    ScoreLine,
    VocalParseScoreEngine,
    project_song_scores,
)
from .engines.text import normalize_lyrics_text, tokenize
from .errors import EngineUnavailableError, ProcessingCancelledError
from .lyrics_document import validate_lyrics_document, words_with_notes
from .lyrics_sources import (
    LyricsDiscovery,
    _lyrics_arrangement_candidates,
    _select_lyrics_arrangement,
    _select_reprise_candidate_at_time,
    discover_lyrics,
)
from .models import StageReport, VocalNote, Word
from .music import analyze_music
from .music_structure import extract_music_structure, find_section_reprise
from .notes import (
    build_vocal_notes,
    constrain_line_final_words_to_voice,
    fit_notes_to_sung_words,
)
from .pitch_post import stabilize_pitch
from .processing_modes import resolve_processing_profile
from .runtime import get_runtime_plan
from .utils.io import write_json_atomic
from .version import AI_BUILD_ID
from .word_voicing import voice_activity_intervals

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AudioPipelineV2Request:
    source_path: str | Path
    output_dir: str | Path
    artist: str
    title: str
    language: str | None = None
    lyrics_path: str | Path | None = None
    genre: str | None = None
    cover_url: str | None = None
    progress: object | None = None
    cancelled: object | None = None
    bpm_override: float | None = None
    key_override: str | None = None
    processing_mode: str = "auto"

    def __post_init__(self) -> None:
        if not str(self.artist or "").strip():
            raise ValueError("Exact artist is required for audio processing")
        if not str(self.title or "").strip():
            raise ValueError("Exact title is required for audio processing")


@dataclass(frozen=True, slots=True)
class AudioPipelineV2Result:
    output_dir: Path
    manifest_path: Path
    warnings: tuple[str, ...]
    reports: tuple[StageReport, ...]


def _reference_word_payload(words: list[Word], notes: list[VocalNote]) -> list[dict]:
    result = words_with_notes(words, notes, owner_only=True)
    for item in result:
        item.pop("confidence", None)
        item.pop("index", None)
    return result


def build_audio_lyrics_document(
    *,
    artist: str,
    title: str,
    text: str,
    bpm: float,
    key: str,
    duration: float,
    words: list[Word],
    notes: list[VocalNote],
) -> dict:
    payload = {
        "schemaVersion": 1,
        "bpm": bpm,
        "duration": round(duration, 3),
        "key": key,
        "reference_audio": "original.flac",
        "text": text,
        "words": _reference_word_payload(words, notes),
        "source": "audio",
        "title": title.strip(),
        "artist": artist.strip(),
    }
    return validate_lyrics_document(payload)


def validate_audio_artifacts(output_dir: str | Path) -> None:
    output = Path(output_dir)
    required = (
        "original.flac", "vocals.flac", "instrumental.flac",
        "lyricsSync.json", "metadata.json", "cover.jpg",
    )
    missing = [name for name in required if not (output / name).is_file()]
    if missing:
        raise ValueError("Missing audio reference artifacts: " + ", ".join(missing))
    audio_info = {}
    for name in ("original.flac", "vocals.flac", "instrumental.flac"):
        info = sf.info(output / name)
        audio_info[name] = info
        if info.frames <= 0:
            raise ValueError(f"{name} is empty")
        if info.channels != 2:
            raise ValueError(f"{name} must be stereo like the reference corpus")
        if info.subtype != "PCM_24":
            raise ValueError(f"{name} must be 24-bit like the reference corpus")
    payload = json.loads((output / "lyricsSync.json").read_text(encoding="utf-8"))
    validate_lyrics_document(payload)
    try:
        document_duration = float(payload["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("lyricsSync.json duration is invalid") from exc
    original_duration = float(audio_info["original.flac"].duration)
    if abs(document_duration - original_duration) > 0.25:
        raise ValueError(
            "lyricsSync.json duration does not match original.flac "
            f"({document_duration:.3f}s vs {original_duration:.3f}s)"
        )
    required_keys = {
        "schemaVersion", "bpm", "duration", "key", "reference_audio",
        "text", "words", "source", "title", "artist",
    }
    if set(payload) != required_keys:
        raise ValueError("lyricsSync.json does not match the reference schema")


class AudioPipelineV2:
    VERSION = f"audio-v2-{AI_BUILD_ID}"

    def __init__(
        self,
        config: CoreConfig | None = None,
        engines: EngineRegistry | None = None,
    ) -> None:
        self.config = config or CoreConfig.from_env()
        self.engines = engines or EngineRegistry.create_default(self.config)

    def close(self) -> None:
        for engine in (
            self.engines.separator, self.engines.pitch,
            self.engines.transcriber, self.engines.aligner,
            getattr(self.engines, "score", None),
        ):
            if engine is None:
                continue
            try:
                getattr(engine, "close", lambda: None)()
            except Exception:
                logger.exception("Failed to close %s", type(engine).__name__)
        release_torch_memory()

    @staticmethod
    def _notify(request: AudioPipelineV2Request, stage: str, percent: float, detail: str) -> None:
        if callable(request.cancelled) and request.cancelled():
            raise ProcessingCancelledError("Song processing cancelled")
        if callable(request.progress):
            request.progress(stage, percent, detail)

    @staticmethod
    def _report(reports: list[StageReport], stage: str, engine: str, started: float) -> None:
        reports.append(StageReport(stage, time.perf_counter() - started, False, engine))

    @staticmethod
    def _normalized_words(values) -> list[Word]:
        result: list[Word] = []
        previous_start = 0.0
        for index, word in enumerate(values):
            start = max(previous_start, float(word.start))
            # The document is serialized with millisecond precision. Keep a
            # full 10 ms minimum so rounding can never collapse a CTC token
            # back into a zero-length interval.
            end = max(start + 0.01, float(word.end))
            result.append(Word(start, end, word.text, word.confidence, index))
            previous_start = start
        return result

    @staticmethod
    def _uses_symbolic_model(processing_mode: str | None) -> bool:
        return str(processing_mode or "auto").strip().lower() == "quality"

    @staticmethod
    def _needs_symbolic_model(
        words: list[Word], notes: list[VocalNote] | tuple[VocalNote, ...]
    ) -> bool:
        """Reserve the heavy score model for genuinely incomplete pitch tracks."""
        if not words:
            return False
        covered = {
            note.word_index for note in notes if note.word_index is not None
        }
        coverage = sum(word.index in covered for word in words) / len(words)
        return coverage < 0.95

    @staticmethod
    def _separation_processing_mode(processing_mode: str | None) -> str:
        # The symbolic quality pass operates on the isolated vocal melody;
        # the slower separation tuning did not improve its reference metrics
        # but added roughly a minute before score generation.
        return "fast"

    @classmethod
    def _keeps_analysis_models_warm(cls, processing_mode: str | None) -> bool:
        return not cls._uses_symbolic_model(processing_mode)

    @classmethod
    def _keeps_separator_warm(cls, processing_mode: str | None) -> bool:
        # MSST occupies enough VRAM to severely slow FCPE/CTC and can make
        # forced alignment hit its timeout. Its worker must be released after
        # separation even in fast mode.
        return False

    def _align(
        self,
        request: AudioPipelineV2Request,
        analysis_vocals: Path,
        discovered: LyricsDiscovery | None,
        structure_audio: Path | None = None,
    ) -> tuple[str, list[Word], str, list[ScoreLine]]:
        if request.lyrics_path and Path(request.lyrics_path).is_file():
            text = Path(request.lyrics_path).read_text(encoding="utf-8-sig").strip()
            discovered = LyricsDiscovery(text, "user", f"{request.artist} - {request.title}")
        if discovered is None:
            text, direct = self.engines.transcriber.transcribe(
                analysis_vocals, request.language
            )
            if not text.strip() or not direct:
                raise EngineUnavailableError("Could not transcribe the complete vocal")
            words = self._normalized_words(direct)
            return text.strip(), words, "asr", self._score_lines(words, text.splitlines())
        discovered = LyricsDiscovery(
            text=normalize_lyrics_text(discovered.text),
            source=discovered.source,
            query=discovered.query,
            language=discovered.language,
            lines=tuple(
                type(line)(line.start, normalize_lyrics_text(line.text))
                for line in discovered.lines
            ),
        )
        if discovered.source != "user":
            source_lines = tuple(
                line.text for line in discovered.lines
            ) or tuple(discovered.text.splitlines())
            candidates = _lyrics_arrangement_candidates(source_lines)
            transcribe_ctc = getattr(self.engines.aligner, "transcribe_ctc", None)
            common_lines = 0
            for rows in zip(*candidates, strict=False):
                if len(set(rows)) != 1:
                    break
                common_lines += 1
            split_seconds = (
                discovered.lines[common_lines].start
                if discovered.lines and common_lines < len(discovered.lines)
                else None
            )
            music_selected = 0
            if (
                len(candidates) > 1
                and structure_audio is not None
                and discovered.lines
                and split_seconds is not None
            ):
                alternative = candidates[1]
                opening_lines = 0
                for position in range(common_lines, len(alternative)):
                    matched = 0
                    while (
                        matched < len(candidates[0])
                        and position + matched < len(alternative)
                        and alternative[position + matched] == candidates[0][matched]
                    ):
                        matched += 1
                    opening_lines = max(opening_lines, matched)
                if opening_lines >= 2 and opening_lines < len(discovered.lines):
                    features, frame_rate, audio_duration = extract_music_structure(
                        structure_audio
                    )
                    template_start = discovered.lines[0].start
                    template_end = discovered.lines[opening_lines].start
                    reprise = find_section_reprise(
                        features,
                        frames_per_second=frame_rate,
                        template_start=template_start,
                        template_end=template_end,
                        search_start=split_seconds,
                        search_end=max(split_seconds, audio_duration - 1.0),
                    )
                    if reprise is not None:
                        music_selected = _select_reprise_candidate_at_time(
                            discovered.lines, candidates, reprise.start
                        )
                        if music_selected:
                            selected = candidates[music_selected]
                            discovered = LyricsDiscovery(
                                text="\n".join(selected),
                                source=f"{discovered.source}+music-arrangement",
                                query=discovered.query,
                                language=discovered.language,
                            )
            if not music_selected and len(candidates) > 1 and callable(transcribe_ctc):
                try:
                    heard_words = transcribe_ctc(analysis_vocals, request.language)
                    heard_text = " ".join(word.text for word in heard_words)
                    selected = _select_lyrics_arrangement(candidates, heard_text)
                except Exception as error:
                    logger.warning(
                        "Acoustic lyrics-arrangement check unavailable: %s", error
                    )
                else:
                    if selected != candidates[0]:
                        discovered = LyricsDiscovery(
                            text="\n".join(selected),
                            source=f"{discovered.source}+audio-arrangement",
                            query=discovered.query,
                            language=discovered.language,
                        )
        text = discovered.text.strip()
        setter = getattr(self.engines.aligner, "set_cancelled", None)
        if callable(setter):
            setter(request.cancelled)
        try:
            if discovered.lines and hasattr(self.engines.aligner, "align_timed_lines"):
                aligned = self.engines.aligner.align_timed_lines(
                    analysis_vocals, text, discovered.lines, request.language
                )
            else:
                aligned = self.engines.aligner.align_long_text(
                    analysis_vocals, text, request.language
                )
        finally:
            if callable(setter):
                setter(None)
        words = self._normalized_words(aligned)
        source_lines = (
            [line.text for line in discovered.lines]
            if discovered.lines else text.splitlines()
        )
        return (
            text,
            words,
            discovered.source,
            self._score_lines(words, source_lines),
        )

    @staticmethod
    def _score_lines(words: list[Word], source_lines: list[str]) -> list[ScoreLine]:
        lines = [line.strip() for line in source_lines if line.strip()]
        counts = [len(tokenize(line)) for line in lines]
        if not lines or sum(counts) != len(words):
            # ASR-only emergency path: short acoustic chunks keep VocalParse
            # inside its reliable line-sized inference window.
            lines, counts = [], []
            for offset in range(0, len(words), 8):
                chunk = words[offset:offset + 8]
                lines.append(" ".join(word.text for word in chunk))
                counts.append(len(chunk))
        result: list[ScoreLine] = []
        cursor = 0
        for line, count in zip(lines, counts, strict=True):
            if count <= 0:
                continue
            first, last = cursor, cursor + count - 1
            next_index = last + 1
            line_end = (
                words[next_index].start
                if next_index < len(words)
                else max(words[last].end, words[last].start + 0.25)
            )
            result.append(ScoreLine(
                line, words[first].start, line_end, first, last
            ))
            cursor += count
        return result

    def run(self, request: AudioPipelineV2Request) -> AudioPipelineV2Result:
        source = Path(request.source_path).resolve()
        output = Path(request.output_dir).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        output.mkdir(parents=True, exist_ok=True)
        reports: list[StageReport] = []
        warnings: list[str] = []
        profile = resolve_processing_profile(
            self._separation_processing_mode(request.processing_mode),
            get_runtime_plan(),
        )

        with tempfile.TemporaryDirectory(prefix=".audio-v2-", dir=output) as temporary:
            work = Path(temporary)
            original = work / "original.flac"
            vocals = work / "vocals.flac"
            instrumental = work / "instrumental.flac"
            analysis_vocals = work / "analysis-vocals.flac"
            lyrics_path = work / "lyricsSync.json"
            metadata_path = work / "metadata.json"
            cover_path = work / "cover.jpg"

            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="audio-v2-net") as pool:
                metadata_future = pool.submit(
                    resolve_audio_metadata,
                    artist=request.artist,
                    title=request.title,
                    genre=request.genre,
                    cover_url=request.cover_url,
                )
                lyrics_future = None if request.lyrics_path else pool.submit(
                    discover_lyrics,
                    request.title,
                    request.artist,
                    complete=True,
                )

                self._notify(request, "decode", 3, "Готовим оригинальную запись")
                started = time.perf_counter()
                decode_audio(source, original, self.config.sample_rate, 2)
                self._report(reports, "decode", "ffmpeg-stereo-24", started)

                self._notify(request, "separate", 10, "Быстро разделяем голос и музыку")
                started = time.perf_counter()
                self.engines.separator.separate(
                    original, vocals, instrumental,
                    profile=profile, cancelled=request.cancelled,
                )
                if not self._keeps_separator_warm(request.processing_mode):
                    getattr(self.engines.separator, "close", lambda: None)()
                self._report(reports, "separate", self.engines.separator.name, started)

                # The published stem remains untouched stereo, matching the
                # reference corpus. Mono exists only as private analysis input.
                decode_audio(vocals, analysis_vocals, self.config.sample_rate, 1)

                self._notify(request, "analysis", 48, "Анализируем темп и мелодию")
                music_future = pool.submit(analyze_music, original)
                pitch = stabilize_pitch(self.engines.pitch.estimate(analysis_vocals))
                if not self._keeps_analysis_models_warm(request.processing_mode):
                    getattr(self.engines.pitch, "close", lambda: None)()
                music = music_future.result()

                self._notify(request, "align", 70, "Точно синхронизируем полный текст")
                discovered = lyrics_future.result() if lyrics_future else None
                text, words, lyric_source, score_lines = self._align(
                    request,
                    analysis_vocals,
                    discovered,
                    original,
                )
                getattr(self.engines.transcriber, "close", lambda: None)()
                if not self._keeps_analysis_models_warm(request.processing_mode):
                    getattr(self.engines.aligner, "close", lambda: None)()
                    release_torch_memory()

                self._notify(request, "notes", 92, "Строим вокальные ноты")
                song_duration = duration(original)
                line_end_indices = frozenset(
                    line.last_word for line in score_lines
                )
                aligned_ends = {word.index: word.end for word in words}
                words = constrain_line_final_words_to_voice(
                    words,
                    voice_activity_intervals(analysis_vocals),
                    line_end_indices=line_end_indices,
                )
                word_end_limits = {
                    word.index: word.end
                    for word in words
                    if word.end + 1e-6 < aligned_ends[word.index]
                }
                physical_notes = build_vocal_notes(
                    pitch,
                    words=words,
                    min_note=self.config.min_note_sec,
                    split_semitones=self.config.split_note_semitones,
                    max_gap=self.config.max_gap_sec,
                    min_confidence=self.config.min_voiced_confidence,
                )
                words, physical_notes = fit_notes_to_sung_words(
                    words,
                    physical_notes,
                    duration=song_duration,
                    word_end_limits=word_end_limits,
                )
                if (
                    self._uses_symbolic_model(request.processing_mode)
                    and self._needs_symbolic_model(words, physical_notes)
                ):
                    if score_lines:
                        final = score_lines[-1]
                        score_lines[-1] = ScoreLine(
                            final.text,
                            final.start,
                            min(song_duration, max(final.end, words[final.last_word].end + 0.25)),
                            final.first_word,
                            final.last_word,
                        )
                    score_engine = getattr(self.engines, "score", None)
                    if score_engine is None:
                        score_engine = VocalParseScoreEngine()
                        self.engines.score = score_engine
                    symbolic_scores = score_engine.transcribe_lines(
                        analysis_vocals,
                        score_lines,
                        cancelled=request.cancelled,
                        progress=lambda completed, total: self._notify(
                            request,
                            "notes",
                            92 + 5 * completed / max(1, total),
                            f"Строим вокальные ноты: {completed}/{total} строк",
                        ),
                    )
                    words, notes = project_song_scores(
                        words,
                        score_lines,
                        symbolic_scores,
                        pitch=pitch,
                        physical_notes=physical_notes,
                    )
                else:
                    notes = physical_notes
                words = self._normalized_words(words)
                payload = build_audio_lyrics_document(
                    artist=request.artist,
                    title=request.title,
                    text=text,
                    bpm=request.bpm_override or music["bpm"],
                    key=request.key_override or music["key"],
                    duration=song_duration,
                    words=words,
                    notes=notes,
                )
                write_json_atomic(lyrics_path, payload, compact=True)

                metadata = metadata_future.result()
                existing_cover = next(
                    (
                        output / f"cover{suffix}"
                        for suffix in (".jpg", ".png", ".webp")
                        if (output / f"cover{suffix}").is_file()
                    ),
                    None,
                )
                cover_ready = bool(
                    existing_cover
                    and normalize_local_cover(existing_cover, cover_path)
                ) or download_cover(metadata.cover_url, cover_path)
                if not cover_ready:
                    raise EngineUnavailableError(
                        "Не удалось получить проверенную обложку для точных исполнителя и названия"
                    )
                metadata_payload = {
                    "dataset_version": 2,
                    "status": "ready",
                    "preparation_mode": "audio-v2",
                    "stems_status": "ready",
                    "title": request.title.strip(),
                    "artist": request.artist.strip(),
                    "genre": metadata.genre,
                    "bpm": payload["bpm"],
                    "key": payload["key"],
                    "duration": payload["duration"],
                    "word_count": len(words),
                    "note_count": len(notes),
                    "lyrics_source": lyric_source,
                    "media": {
                        "cover_status": "ready",
                        "video_status": "pending",
                    },
                    "warnings": warnings,
                    "files": [
                        "cover.jpg", "instrumental.flac", "lyricsSync.json",
                        "metadata.json", "original.flac", "vocals.flac",
                    ],
                }
                write_json_atomic(metadata_path, metadata_payload, compact=False)

            self._notify(request, "validate", 98, "Сверяем полный комплект результата")
            publish_files_atomically([
                (original, output / "original.flac"),
                (vocals, output / "vocals.flac"),
                (instrumental, output / "instrumental.flac"),
                (lyrics_path, output / "lyricsSync.json"),
                (metadata_path, output / "metadata.json"),
                (cover_path, output / "cover.jpg"),
            ])
            validate_audio_artifacts(output)

        return AudioPipelineV2Result(output, output / "lyricsSync.json", tuple(warnings), tuple(reports))

    def separate_stems(
        self,
        source_path: str | Path,
        vocals_path: str | Path,
        instrumental_path: str | Path,
        *,
        processing_mode: str = "fast",
    ) -> None:
        profile = resolve_processing_profile(processing_mode, get_runtime_plan())
        self.engines.separator.separate(
            Path(source_path), Path(vocals_path), Path(instrumental_path), profile=profile
        )
