from __future__ import annotations

import json
import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
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
    TimedLine,
    _lyrics_arrangement_candidates,
    _retime_arrangement_lines,
    _select_lyrics_arrangement,
    _select_reprise_candidate_at_time,
    discover_lyrics,
)
from .models import StageReport, VocalNote, Word
from .music import analyze_music
from .music_structure import (
    extract_music_structure,
    find_partial_section_reprise,
    find_section_reprise,
)
from .notes import (
    build_vocal_notes,
    constrain_line_final_words_to_voice,
    fit_notes_with_refined_ownership,
)
from .pitch_post import stabilize_pitch
from .processing_modes import resolve_processing_profile
from .runtime import get_runtime_plan
from .utils.io import write_json_atomic
from .version import AI_BUILD_ID
from .word_voicing import anchor_words_to_voice, voice_activity_intervals

logger = logging.getLogger(__name__)

_MIN_AUDIO_WORD_SECONDS = 0.04


def _note_line_start_indices(lines: list[ScoreLine]) -> frozenset[int]:
    return frozenset(
        line.first_word
        for line in lines
        if line.first_word == line.last_word
    )


def _word_end_limits(
    words: list[Word],
    *,
    aligned_ends: dict[int, float],
    line_end_indices: set[int] | frozenset[int],
) -> dict[int, float]:
    return {
        word.index: word.end
        for word in words
        if (
            word.end + 1e-6 < aligned_ends[word.index]
            or (
                word.index in line_end_indices
                and aligned_ends[word.index] - word.start >= 0.5
            )
        )
    }


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
    words = payload["words"]
    if str(payload.get("text") or "").strip() and not words:
        raise ValueError("lyricsSync.json has no synchronized words")
    for index in range(1, len(words)):
        if float(words[index - 1]["end"]) > float(words[index]["start"]) + 1e-6:
            raise ValueError(
                f"lyricsSync.json has overlapping words {index - 1} and {index}"
            )
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
    melody_coverage = (
        sum(bool(word.get("notes")) for word in words) / len(words)
        if words
        else 1.0
    )
    if words and melody_coverage < 0.15:
        raise ValueError("lyricsSync.json has insufficient melody notes")
    required_keys = {
        "schemaVersion", "bpm", "duration", "key", "reference_audio",
        "text", "words", "source", "title", "artist",
    }
    if set(payload) != required_keys:
        raise ValueError("lyricsSync.json does not match the reference schema")


class AudioPipelineV2:
    VERSION = f"audio-v2-{AI_BUILD_ID}"

    @staticmethod
    def _parallel_worker_count() -> int:
        # Cover and lyrics are network-bound and can both remain in flight
        # when separation finishes.  Keep one independent slot available for
        # local tempo/key analysis so slow internet never serializes it.
        return 3

    @staticmethod
    def _background_result(future, *, cancelled=None):
        if not callable(cancelled):
            return future.result()
        while True:
            if cancelled():
                future.cancel()
                raise ProcessingCancelledError("Song processing cancelled")
            try:
                return future.result(timeout=0.1)
            except FutureTimeoutError:
                continue

    @staticmethod
    def _download_cover_candidates(
        urls,
        destination: str | Path,
        *,
        downloader=download_cover,
    ) -> bool:
        return any(downloader(url, destination) for url in dict.fromkeys(urls) if url)

    @staticmethod
    @contextmanager
    def _background_pool():
        pool = ThreadPoolExecutor(
            max_workers=AudioPipelineV2._parallel_worker_count(),
            thread_name_prefix="audio-v2-work",
        )
        try:
            yield pool
        except ProcessingCancelledError:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True, cancel_futures=True)

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
    def _discover_lyrics_reliably(
        title: str | None,
        artist: str | None,
        duration_seconds: float | None = None,
    ) -> LyricsDiscovery | None:
        for attempt in range(2):
            try:
                options = {"complete": True}
                if duration_seconds is not None:
                    options["duration_seconds"] = duration_seconds
                result = discover_lyrics(title, artist, **options)
            except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                logger.info(
                    "Lyrics lookup attempt %s failed for %r - %r: %s",
                    attempt + 1,
                    artist,
                    title,
                    error,
                )
                result = None
            if result is not None:
                return result
        return None

    @staticmethod
    def _cached_lyrics(
        output_dir: str | Path, *, artist: str, title: str
    ) -> LyricsDiscovery | None:
        output = Path(output_dir)
        try:
            metadata = json.loads(
                (output / "metadata.json").read_text(encoding="utf-8")
            )
            payload = json.loads(
                (output / "lyricsSync.json").read_text(encoding="utf-8")
            )
            validate_lyrics_document(payload)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

        def identity(value):
            return " ".join(str(value or "").split()).casefold()
        source = str(metadata.get("lyrics_source") or "").strip()
        if (
            metadata.get("preparation_mode") != "audio-v2"
            or identity(metadata.get("artist")) != identity(artist)
            or identity(metadata.get("title")) != identity(title)
            or identity(payload.get("artist")) != identity(artist)
            or identity(payload.get("title")) != identity(title)
            or not source
            or source.casefold() == "asr"
        ):
            return None
        text = str(payload.get("text") or "").strip()
        if not text:
            return None
        while source.casefold().startswith("cache:"):
            source = source.split(":", 1)[1].strip()
        return LyricsDiscovery(
            text=text,
            source=f"cache:{source}",
            query=f"{artist} - {title}",
        )

    @staticmethod
    def _resolve_discovered_lyrics(
        discovered: LyricsDiscovery | None,
        cached: LyricsDiscovery | None,
    ) -> LyricsDiscovery | None:
        return discovered if discovered is not None else cached

    @staticmethod
    def _normalized_words(
        values, *, duration: float | None = None
    ) -> list[Word]:
        ordered: list[Word] = []
        previous_start: float | None = None
        for index, word in enumerate(values):
            minimum_start = (
                0.0
                if previous_start is None
                else previous_start + _MIN_AUDIO_WORD_SECONDS
            )
            start = max(minimum_start, float(word.start))
            # The reference corpus never flashes a word for less than 40 ms.
            # CTC can collapse neighbouring short tokens onto one timestamp;
            # keep them visible while retaining their original order.
            end = max(start + _MIN_AUDIO_WORD_SECONDS, float(word.end))
            ordered.append(Word(start, end, word.text, word.confidence, index))
            previous_start = start

        result: list[Word] = []
        for index, word in enumerate(ordered):
            next_start = (
                ordered[index + 1].start
                if index + 1 < len(ordered)
                else None
            )
            end = min(word.end, next_start) if next_start is not None else word.end
            result.append(
                Word(word.start, end, word.text, word.confidence, word.index)
            )
        if duration is not None and result and result[-1].end > duration:
            bounded: list[Word] = []
            end_limit = float(duration)
            for word in reversed(result):
                end = min(word.end, end_limit)
                start = min(word.start, end - _MIN_AUDIO_WORD_SECONDS)
                if start < 0.0:
                    raise EngineUnavailableError(
                        "Aligned words cannot fit inside the audio duration"
                    )
                bounded.append(
                    Word(start, end, word.text, word.confidence, word.index)
                )
                end_limit = start
            result = list(reversed(bounded))
        return result

    @staticmethod
    def _uses_symbolic_model(processing_mode: str | None) -> bool:
        return str(processing_mode or "auto").strip().lower() == "quality"

    @staticmethod
    def _melody_coverage(
        words: list[Word], notes: list[VocalNote] | tuple[VocalNote, ...]
    ) -> float:
        if not words:
            return 1.0
        covered = {
            note.word_index for note in notes if note.word_index is not None
        }
        return sum(word.index in covered for word in words) / len(words)

    @classmethod
    def _needs_symbolic_model(
        cls, words: list[Word], notes: list[VocalNote] | tuple[VocalNote, ...]
    ) -> bool:
        """Reserve the heavy score model for genuinely incomplete pitch tracks."""
        if not words:
            return False
        return cls._melody_coverage(words, notes) < 0.95

    @classmethod
    def _should_use_symbolic_model(
        cls,
        processing_mode: str | None,
        words: list[Word],
        notes: list[VocalNote] | tuple[VocalNote, ...],
    ) -> bool:
        """Use the costly model for quality mode or as a catastrophic rescue.

        Normal ``auto`` processing stays on the fast physical-pitch path.  A
        catastrophically sparse pitch track is different: publishing it would
        create karaoke with lyrics but effectively no melody, so the symbolic
        model is allowed as a last-resort recovery even in auto mode.
        """
        if not words:
            return False
        if cls._uses_symbolic_model(processing_mode):
            return cls._needs_symbolic_model(words, notes)
        return cls._melody_coverage(words, notes) < 0.15

    @classmethod
    def _require_complete_melody(
        cls, words: list[Word], notes: list[VocalNote] | tuple[VocalNote, ...]
    ) -> None:
        if not words:
            raise EngineUnavailableError(
                "Не удалось построить синхронизированные слова песни"
            )
        if cls._melody_coverage(words, notes) < 0.15:
            raise EngineUnavailableError(
                "Не удалось построить мелодические ноты вокала"
            )

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
            context_artist = str(request.artist or "").strip()
            context_title = str(request.title or "").strip()
            asr_context = (
                f"{context_artist} — {context_title}"
                if context_artist and context_title
                else ""
            )
            text, direct = self.engines.transcriber.transcribe(
                analysis_vocals,
                request.language,
                context=asr_context,
            )
            text = text.strip()
            if not text and direct:
                text = " ".join(word.text for word in direct).strip()
            if not text:
                raise EngineUnavailableError("Could not transcribe the complete vocal")
            if direct:
                words = self._normalized_words(direct)
                return text, words, "asr", self._score_lines(words, text.splitlines())
            # Neural ASR can return the complete transcription without word
            # timestamps (notably when audio is processed in several voice
            # chunks).  The text is still valid input for the forced aligner;
            # discarding it here made otherwise healthy songs fail outright.
            timed_rows = tuple(
                TimedLine(float(start), normalize_lyrics_text(line))
                for start, line in getattr(
                    self.engines.transcriber, "last_timed_lines", ()
                )
                if str(line).strip()
            )
            if normalize_lyrics_text("\n".join(line.text for line in timed_rows)) != normalize_lyrics_text(text):
                timed_rows = ()
            discovered = LyricsDiscovery(
                text,
                "asr",
                f"{request.artist} - {request.title}",
                language=request.language,
                lines=timed_rows,
            )
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
        if discovered.source not in {"user", "asr"}:
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
                    if reprise is None:
                        reprise = find_partial_section_reprise(
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
                                lines=_retime_arrangement_lines(
                                    discovered.lines,
                                    selected,
                                    duration=audio_duration,
                                ),
                            )
            if (
                not music_selected
                and len(candidates) > 1
                and callable(transcribe_ctc)
            ):
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
                            lines=_retime_arrangement_lines(
                                discovered.lines,
                                selected,
                                duration=duration(analysis_vocals),
                            ),
                        )
                finally:
                    # transcribe_ctc runs in the parent process, whereas the
                    # subsequent forced alignment normally loads its model in
                    # an isolated worker.  Leaving the probe resident on the
                    # GPU can therefore require two copies of the same large
                    # CTC model and fail with CUDA OOM.
                    parker = getattr(self.engines.aligner, "park", None)
                    if callable(parker):
                        parker()
                    release_torch_memory()
        text = discovered.text.strip()
        setter = getattr(self.engines.aligner, "set_cancelled", None)
        if callable(setter):
            setter(request.cancelled)
        try:
            if (
                discovered.source == "asr"
                and discovered.lines
                and hasattr(self.engines.aligner, "align_chunked_lines")
            ):
                aligned = self.engines.aligner.align_chunked_lines(
                    analysis_vocals, text, discovered.lines, request.language
                )
            elif discovered.lines and hasattr(self.engines.aligner, "align_timed_lines"):
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
        if getattr(self.engines.aligner, "needs_voice_anchoring", False):
            aligned = anchor_words_to_voice(
                aligned,
                voice_activity_intervals(analysis_vocals),
                duration(analysis_vocals),
            )
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
            line_start = words[first].start
            line_end = (
                max(
                    words[next_index].start,
                    words[last].end,
                    line_start + 0.01,
                )
                if next_index < len(words)
                else max(words[last].end, words[last].start + 0.25)
            )
            result.append(ScoreLine(
                line, line_start, line_end, first, last
            ))
            cursor += count
        return result

    def _fit_song_notes(
        self,
        request: AudioPipelineV2Request,
        analysis_vocals: Path,
        *,
        song_duration: float,
        words: list[Word],
        score_lines: list[ScoreLine],
        pitch,
    ) -> tuple[list[Word], list[VocalNote]]:
        line_end_indices = frozenset(line.last_word for line in score_lines)
        aligned_ends = {word.index: word.end for word in words}
        words = constrain_line_final_words_to_voice(
            words,
            voice_activity_intervals(analysis_vocals),
            line_end_indices=line_end_indices,
        )
        word_end_limits = _word_end_limits(
            words,
            aligned_ends=aligned_ends,
            line_end_indices=line_end_indices,
        )
        note_options = {
            "min_note": self.config.min_note_sec,
            "split_semitones": self.config.split_note_semitones,
            "max_gap": self.config.max_gap_sec,
            "min_confidence": self.config.min_voiced_confidence,
            "line_start_indices": _note_line_start_indices(score_lines),
        }
        physical_notes = build_vocal_notes(
            pitch,
            words=words,
            **note_options,
        )
        words, physical_notes = fit_notes_with_refined_ownership(
            words,
            physical_notes,
            pitch_frames=pitch,
            note_options=note_options,
            duration=song_duration,
            word_end_limits=word_end_limits,
        )
        if self._should_use_symbolic_model(
            request.processing_mode, words, physical_notes
        ):
            if score_lines:
                final = score_lines[-1]
                score_lines[-1] = ScoreLine(
                    final.text,
                    final.start,
                    min(
                        song_duration,
                        max(final.end, words[final.last_word].end + 0.25),
                    ),
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
        words = self._normalized_words(words, duration=song_duration)
        self._require_complete_melody(words, notes)
        return words, notes

    def run(self, request: AudioPipelineV2Request) -> AudioPipelineV2Result:
        source = Path(request.source_path).resolve()
        output = Path(request.output_dir).resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        output.mkdir(parents=True, exist_ok=True)
        cached_lyrics = (
            None
            if request.lyrics_path
            else self._cached_lyrics(
                output,
                artist=request.artist,
                title=request.title,
            )
        )
        reports: list[StageReport] = []
        warnings: list[str] = []
        profile = resolve_processing_profile(
            self._separation_processing_mode(request.processing_mode),
            get_runtime_plan(),
        )
        source_duration = duration(source)

        with tempfile.TemporaryDirectory(prefix=".audio-v2-", dir=output) as temporary:
            work = Path(temporary)
            original = work / "original.flac"
            vocals = work / "vocals.flac"
            instrumental = work / "instrumental.flac"
            analysis_vocals = work / "analysis-vocals.flac"
            lyrics_path = work / "lyricsSync.json"
            metadata_path = work / "metadata.json"
            cover_path = work / "cover.jpg"

            with self._background_pool() as pool:
                metadata_future = pool.submit(
                    resolve_audio_metadata,
                    artist=request.artist,
                    title=request.title,
                    genre=request.genre,
                    cover_url=request.cover_url,
                )
                lyrics_future = None if request.lyrics_path else pool.submit(
                    self._discover_lyrics_reliably,
                    request.title,
                    request.artist,
                    source_duration,
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
                started = time.perf_counter()
                music_future = pool.submit(analyze_music, original)
                pitch = stabilize_pitch(self.engines.pitch.estimate(analysis_vocals))
                if not self._keeps_analysis_models_warm(request.processing_mode):
                    getattr(self.engines.pitch, "close", lambda: None)()
                music = self._background_result(
                    music_future, cancelled=request.cancelled
                )
                self._report(reports, "analysis", self.engines.pitch.name, started)

                self._notify(request, "align", 70, "Точно синхронизируем полный текст")
                started = time.perf_counter()
                discovered = self._resolve_discovered_lyrics(
                    (
                        self._background_result(
                            lyrics_future, cancelled=request.cancelled
                        )
                        if lyrics_future
                        else None
                    ),
                    cached_lyrics,
                )
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
                self._report(reports, "align", self.engines.aligner.name, started)

                self._notify(request, "notes", 92, "Строим вокальные ноты")
                started = time.perf_counter()
                song_duration = duration(original)
                words, notes = self._fit_song_notes(
                    request,
                    analysis_vocals,
                    song_duration=song_duration,
                    words=words,
                    score_lines=score_lines,
                    pitch=pitch,
                )
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
                self._report(reports, "notes", "fcpe-segments", started)

                started = time.perf_counter()
                metadata = self._background_result(
                    metadata_future, cancelled=request.cancelled
                )
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
                ) or self._download_cover_candidates(
                    metadata.cover_urls or (metadata.cover_url,),
                    cover_path,
                )
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
                self._report(reports, "metadata", "online-metadata", started)

            self._notify(request, "validate", 98, "Сверяем полный комплект результата")
            started = time.perf_counter()
            publish_files_atomically([
                (original, output / "original.flac"),
                (vocals, output / "vocals.flac"),
                (instrumental, output / "instrumental.flac"),
                (lyrics_path, output / "lyricsSync.json"),
                (metadata_path, output / "metadata.json"),
                (cover_path, output / "cover.jpg"),
            ])
            validate_audio_artifacts(output)
            self._report(reports, "validate", "artifact-contract", started)

        return AudioPipelineV2Result(output, output / "lyricsSync.json", tuple(warnings), tuple(reports))

    def reprocess_song(
        self,
        output_dir: str | Path,
        *,
        artist: str | None = None,
        title: str | None = None,
        language: str | None = None,
        progress=None,
        cancelled=None,
        **_options,
    ) -> AudioPipelineV2Result:
        """Rebuild words and melody from existing audio-v2 stems.

        This is the implementation behind "Reprocess melody".  It deliberately
        keeps original/vocals/instrumental untouched and uses exactly the same
        alignment and physical-note fitting path as a new ordinary-audio job.
        """
        output = Path(output_dir).resolve()
        original = output / "original.flac"
        vocals = output / "vocals.flac"
        lyrics_path = output / "lyricsSync.json"
        metadata_path = output / "metadata.json"
        required = (
            original,
            vocals,
            output / "instrumental.flac",
            lyrics_path,
            metadata_path,
            output / "cover.jpg",
        )
        missing = [path.name for path in required if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                "Existing audio-v2 artifacts are required: " + ", ".join(missing)
            )
        current = validate_lyrics_document(
            json.loads(lyrics_path.read_text(encoding="utf-8"))
        )
        loaded_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = loaded_metadata if isinstance(loaded_metadata, dict) else {}
        resolved_artist = str(artist or current.get("artist") or "").strip()
        resolved_title = str(title or current.get("title") or "").strip()
        text = str(current.get("text") or "").strip()
        if not text:
            raise EngineUnavailableError("lyricsSync.json has no text to align")
        request = AudioPipelineV2Request(
            original,
            output,
            artist=resolved_artist,
            title=resolved_title,
            language=language,
            processing_mode="auto",
            progress=progress,
            cancelled=cancelled,
        )
        reports: list[StageReport] = []
        with tempfile.TemporaryDirectory(prefix=".audio-v2-reprocess-", dir=output) as temporary:
            work = Path(temporary)
            analysis_vocals = work / "analysis-vocals.flac"
            next_lyrics = work / "lyricsSync.json"
            next_metadata = work / "metadata.json"
            decode_audio(vocals, analysis_vocals, self.config.sample_rate, 1)

            self._notify(request, "analysis", 48, "Анализируем мелодию голоса")
            started = time.perf_counter()
            pitch = stabilize_pitch(self.engines.pitch.estimate(analysis_vocals))
            self._report(reports, "analysis", self.engines.pitch.name, started)

            self._notify(
                request,
                "align",
                70,
                "Сохраняем проверенную синхронизацию слов",
            )
            words = [
                Word(
                    float(item["start"]),
                    float(item["end"]),
                    str(item["text"]),
                    1.0,
                    index,
                )
                for index, item in enumerate(current["words"])
            ]
            aligned_text = text
            lyric_source = str(metadata.get("lyrics_source") or "existing")
            score_lines = self._score_lines(words, text.splitlines())

            self._notify(request, "notes", 92, "Строим вокальные ноты")
            started = time.perf_counter()
            song_duration = duration(original)
            words, notes = self._fit_song_notes(
                request,
                analysis_vocals,
                song_duration=song_duration,
                words=words,
                score_lines=score_lines,
                pitch=pitch,
            )
            self._report(reports, "notes", "fcpe-segments", started)
            payload = build_audio_lyrics_document(
                artist=resolved_artist,
                title=resolved_title,
                text=aligned_text,
                bpm=float(current.get("bpm") or 120.0),
                key=str(current.get("key") or "C"),
                duration=song_duration,
                words=words,
                notes=notes,
            )
            write_json_atomic(next_lyrics, payload, compact=True)
            metadata.update({
                "dataset_version": 2,
                "status": "ready",
                "preparation_mode": "audio-v2",
                "artist": resolved_artist,
                "title": resolved_title,
                "duration": payload["duration"],
                "word_count": len(words),
                "note_count": len(notes),
                "lyrics_source": lyric_source,
            })
            write_json_atomic(next_metadata, metadata, compact=False)
            publish_files_atomically([
                (next_lyrics, lyrics_path),
                (next_metadata, metadata_path),
            ])
        validate_audio_artifacts(output)
        self._notify(request, "complete", 100, "Готово")
        return AudioPipelineV2Result(
            output,
            lyrics_path,
            (),
            tuple(reports),
        )

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
