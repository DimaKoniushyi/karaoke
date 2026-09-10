from __future__ import annotations

import os
import re
import unicodedata
from math import ceil

from ..audio import duration
from ..errors import EngineUnavailableError, InvalidArtifactError
from ..models import Word
from .alignment_process import IsolatedAlignerMixin
from .alignment_tokens import reconcile_words
from .asr_support import asr_voice_chunks as _asr_voice_chunks
from .asr_support import context_echo_key as _context_echo_key
from .asr_support import greedy_generation_config as _greedy_generation_config
from .asr_support import prepare_greedy_generation as _prepare_greedy_generation
from .base import Aligner, Transcriber
from .ctc_selection import select_stronger_ctc_alignment as _select_stronger_ctc_alignment
from .ctc_text import (
    create_ctc_aligner as _create_ctc_aligner,
)
from .ctc_text import (
    ctc_language_encodable as _ctc_language_encodable,
)
from .ctc_text import (
    ctc_tokens as _ctc_tokens,
)
from .device import select_torch_device
from .timed_alignment import (
    acoustic_runs as _acoustic_runs,
)
from .timed_alignment import (
    coarse_line_starts as _coarse_line_starts,
)
from .timed_alignment import (
    context_groups as _context_groups,
)
from .timed_alignment import (
    ctc_windows as _ctc_windows,
)
from .timed_alignment import (
    enforce_monotonic_starts as _enforce_monotonic_starts,
)
from .timed_alignment import (
    fill_unresolved_timed_lines as _fill_unresolved_timed_lines,
)
from .timed_alignment import (
    invalid_runs as _invalid_runs,
)
from .timed_alignment import (
    repair_bounds as _repair_bounds,
)
from .timed_alignment import (
    repair_collapsed_timed_lines as _repair_collapsed_timed_lines,
)
from .timed_alignment import (
    runs as _runs,
)
from .timed_alignment import (
    timed_line_retry_stages as _timed_line_retry_stages,
)
from .timed_line_repairs import repair_timed_line_outliers as _repair_timed_line_outliers

ASR_PIPELINE_VERSION = "clean-v1"
LONG_TEXT_ALIGNMENT_VERSION = "clean-v2"
LANGUAGES = {"en": "English", "ru": "Russian", "uk": "Ukrainian"}
WINDOWED_ALIGNMENT_THRESHOLD_SECONDS = 600.0


def _relabel_ctc_words(words: list[Word], tokens: list[str], offset: int = 0) -> list[Word]:
    if len(words) != len(tokens):
        raise InvalidArtifactError(f"CTC returned {len(words)} words for {len(tokens)} tokens")
    return [
        Word(word.start, word.end, token, word.confidence, offset + index)
        for index, (word, token) in enumerate(zip(words, tokens, strict=True))
    ]


def _interpolate_invalid_words(words: list[Word], tokens: list[str], span: float) -> list[Word]:
    """Last-resort local repair used when an optional CTC pass cannot encode a token."""
    for start, end in _invalid_runs(words, span):
        previous = words[start - 1] if start else None
        following = words[end] if end < len(words) else None
        lower = previous.end if previous is not None else 0.0
        upper = following.start if following is not None else span
        if upper <= lower:
            lower = previous.start if previous is not None else 0.0
            upper = following.start if following is not None else span

        if upper <= lower:
            anchor = min(max(lower, 0.0), span)
            fallback_end = min(span + 0.05, anchor + 0.01)
            for index in range(start, end):
                words[index] = Word(anchor, fallback_end, tokens[index], 0.0, index)
            continue

        weights = [
            max(1, sum(char.isalnum() for char in tokens[index])) for index in range(start, end)
        ]
        total, consumed, cursor = sum(weights), 0, lower
        for index, weight in zip(range(start, end), weights, strict=True):
            consumed += weight
            boundary = upper if index == end - 1 else lower + (upper - lower) * consumed / total
            words[index] = Word(cursor, boundary, tokens[index], 0.0, index)
            cursor = boundary
    return words


_LATIN_CYRILLIC_HOMOGLYPHS = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "C": "С",
        "E": "Е",
        "H": "Н",
        "K": "К",
        "M": "М",
        "O": "О",
        "P": "Р",
        "T": "Т",
        "X": "Х",
        "Y": "У",
        "a": "а",
        "c": "с",
        "e": "е",
        "o": "о",
        "p": "р",
        "x": "х",
        "y": "у",
    }
)
_REPEATED_HYPHEN_WORD_RE = re.compile(
    r"(?<!\w)([^\W\d_]+)[\-‐‑‒–—]\1(?!\w)",
    flags=re.IGNORECASE | re.UNICODE,
)
_SUNG_VOCALISATION_RE = re.compile(
    r"^[^\W\d_]{1,2}(?:[\-‐‑‒–—][^\W\d_]{1,2}){2,}$",
    flags=re.UNICODE,
)


def normalize_lyrics_text(text: str) -> str:
    """Repair provider encoding artifacts without rewriting real Latin words."""

    def clean_token(match: re.Match[str]) -> str:
        token = match.group(0)
        has_cyrillic = any(
            "а" <= char.casefold() <= "я" or char.casefold() in "ёіїєґ" for char in token
        )
        has_latin = any("a" <= char.casefold() <= "z" for char in token)
        if has_cyrillic and has_latin:
            token = token.translate(_LATIN_CYRILLIC_HOMOGLYPHS)
        if _SUNG_VOCALISATION_RE.fullmatch(token):
            return re.sub(r"[\-‐‑‒–—]", " ", token)
        return _REPEATED_HYPHEN_WORD_RE.sub(r"\1 \1", token)

    cleaned = re.sub(r"\S+", clean_token, text)

    def compact_vocalisation(line: str) -> str:
        parts = line.split()
        vowels = frozenset("аеёиоуыэюяіїє")
        if len(parts) < 4 or not all(
            1 <= len(part) <= 2 and all(char.casefold() in vowels for char in part)
            for part in parts
        ):
            return line
        compacted: list[str] = []
        index = 0
        while index < len(parts):
            if (
                parts[index].casefold() in {"и", "і"}
                and index + 1 < len(parts)
                and len(parts[index + 1]) == 1
            ):
                compacted.append(parts[index] + parts[index + 1].casefold())
                index += 2
            else:
                compacted.append(parts[index])
                index += 1
        return " ".join(compacted)

    return "\n".join(compact_vocalisation(line) for line in cleaned.split("\n"))


def tokenize(text: str) -> list[str]:
    def kept(char: str) -> bool:
        return char == "'" or unicodedata.category(char)[:1] in {"L", "N"}

    result = []
    for part in normalize_lyrics_text(text).split():
        positions = [index for index, char in enumerate(part) if kept(char)]
        if positions:
            result.append(part[positions[0] : positions[-1] + 1])
    return result


def resolve_alignment_language(text: str, language: str | None = None) -> str:
    lowered = text.lower()
    if any(char in lowered for char in "іїєґ"):
        return "Ukrainian"
    if language:
        value = language.split("-")[0].lower()
        return LANGUAGES.get(value, language)
    return "Russian" if any("а" <= char <= "я" or char == "ё" for char in lowered) else "English"


def _items(value):
    if isinstance(value, dict):
        return value.get("words") or value.get("items") or value.get("segments") or []
    if hasattr(value, "items"):
        return value.items
    return value if isinstance(value, (list, tuple)) else []


def _words(value) -> list[Word]:
    result = []
    for index, item in enumerate(_items(value)):
        data = (
            item
            if isinstance(item, dict)
            else vars(item)
            if hasattr(item, "__dict__")
            else {
                "text": getattr(item, "text", ""),
                "start_time": getattr(item, "start_time", None),
                "end_time": getattr(item, "end_time", None),
            }
        )
        text = str(data.get("text") or data.get("word") or "").strip()
        try:
            result.append(
                Word(
                    float(data.get("start", data.get("start_time"))),
                    float(data.get("end", data.get("end_time"))),
                    text,
                    float(data.get("confidence", 1)),
                    index,
                )
            )
        except (TypeError, ValueError):
            continue
    return result


def _invalid(word: Word, span: float) -> bool:
    return word.start < 0 or word.end <= word.start or word.end > span + 0.1


def _timed_line_plan(text: str, lines, span: float):
    tokens, entries, flattened = tokenize(text), [], []
    for line in lines:
        lower, line_tokens = len(flattened), tokenize(line.text)
        flattened.extend(line_tokens)
        entries.append((float(line.start), lower, len(flattened)))

    def normalized(values):
        return [
            "".join(char for char in value.casefold() if char.isalnum() or char == "'")
            for value in values
        ]

    if normalized(flattened) != normalized(tokens):
        raise InvalidArtifactError("Synchronized lyric lines do not match canonical lyrics")

    groups, first = [], 0
    while first < len(entries):
        last = first + 1
        while last < len(entries) and entries[last][0] - entries[first][0] < 24:
            last += 1
        start = max(0, entries[first][0] - 0.75)
        end = min(span, (entries[last][0] if last < len(entries) else span) + 0.75)
        groups.append((entries[first][1], entries[last - 1][2], start, end))
        first = last
    return tokens, entries, groups


def _interpolate_entries(
    tokens: list[str], entries: list[tuple[float, int, int]], span: float
) -> list[Word]:
    words: list[Word | None] = [None] * len(tokens)
    _fill_unresolved_timed_lines(words, entries, tokens, span)
    if any(word is None for word in words):
        raise InvalidArtifactError("Lyric timing windows do not cover every word")
    result = [word for word in words if word is not None]
    _enforce_monotonic_starts(result, span)
    return result


def _coarse_entries(text: str, tokens: list[str], voice_intervals, span: float):
    counts: list[int] = []
    for line in text.splitlines():
        remaining = len(tokenize(line))
        while remaining:
            size = min(8, remaining)
            counts.append(size)
            remaining -= size
    if not counts or sum(counts) != len(tokens):
        counts = [min(8, len(tokens) - offset) for offset in range(0, len(tokens), 8)]
    starts = _coarse_line_starts(counts, list(voice_intervals), span=span)
    entries, cursor = [], 0
    for start, count in zip(starts, counts, strict=True):
        entries.append((start, cursor, cursor + count))
        cursor += count
    return entries


def _timed_line_offset(samples, rate: int, lines, span: float) -> float:
    """Estimate a changed intro length from the isolated vocal energy."""
    import numpy as np

    if len(lines) < 2 or rate <= 0 or span <= 0:
        return 0.0
    frame_seconds = 0.1
    frame_size = max(1, round(rate * frame_seconds))
    signal = np.asarray(samples, dtype=np.float32).reshape(-1)
    padding = (-len(signal)) % frame_size
    if padding:
        signal = np.pad(signal, (0, padding))
    energy = np.sqrt(np.mean(signal.reshape(-1, frame_size) ** 2, axis=1) + 1e-10)
    energy = np.log1p(energy * 100.0)
    prefix = np.concatenate(([0.0], np.cumsum(energy)))
    starts = [float(line.start) for line in lines]
    word_counts = [max(1, len(tokenize(str(line.text)))) for line in lines]

    def score(offset: float) -> float:
        total = frames = valid = 0
        for index, (start, count) in enumerate(zip(starts, word_counts, strict=True)):
            shifted = start + offset
            following = starts[index + 1] + offset if index + 1 < len(starts) else span
            end = min(following, shifted + max(1.0, 0.45 * count))
            lower = max(0, round(shifted / frame_seconds))
            upper = min(len(energy), round(end / frame_seconds))
            if upper <= lower:
                continue
            total += prefix[upper] - prefix[lower]
            frames += upper - lower
            valid += 1
        if valid < max(2, round(len(starts) * 0.8)) or not frames:
            return float("-inf")
        return float(total / frames) - abs(offset) * 0.0005

    limit = min(180.0, span)
    candidates = np.arange(-limit, limit + frame_seconds / 2, frame_seconds)
    best = max((score(float(offset)), float(offset)) for offset in candidates)
    baseline = score(0.0)
    if not np.isfinite(best[0]) or best[0] < baseline + max(0.05, abs(baseline) * 0.08):
        return 0.0
    return round(best[1], 3)


def _load(model_class, name, role, **options):
    import torch

    device = select_torch_device(torch, role)
    return model_class.from_pretrained(
        name,
        device_map=device,
        dtype=torch.float16 if device == "cuda" else torch.float32,
        **options,
    )


def _activate_loaded(wrapper, role):
    if wrapper is None or getattr(wrapper, "backend", "transformers") != "transformers":
        return wrapper
    import torch

    device = select_torch_device(torch, role)
    model = getattr(wrapper, "model", None)
    if model is not None and str(getattr(model, "device", "")) != device:
        model.to(device)
        if hasattr(wrapper, "device"):
            wrapper.device = getattr(model, "device", device)
    return wrapper


def _park_loaded(wrapper):
    if wrapper is None or getattr(wrapper, "backend", "transformers") != "transformers":
        return
    model = getattr(wrapper, "model", None)
    if model is not None and str(getattr(model, "device", "")) != "cpu":
        model.to("cpu")
        if hasattr(wrapper, "device"):
            wrapper.device = getattr(model, "device", "cpu")


class Qwen3Transcriber(Transcriber):
    name = "qwen3-asr"

    def __init__(self, model: str):
        self.model_name, self._model, self._ctc = model, None, {}
        self.last_timed_lines: tuple[tuple[float, str], ...] = ()

    def _load(self):
        try:
            from qwen_asr import Qwen3ASRModel
        except ImportError as error:
            raise EngineUnavailableError("qwen-asr is unavailable") from error
        if self._model is None:
            load_options = {
                "max_inference_batch_size": 2,
                "max_new_tokens": 192,
            }
            generation_config = _greedy_generation_config(self.model_name)
            if generation_config is not None:
                load_options["generation_config"] = generation_config
            self._model = _load(
                Qwen3ASRModel,
                self.model_name,
                "asr",
                # Two concurrent chunks keep the GPU responsive for the
                # desktop compositor/browser while retaining most of the
                # throughput benefit over one whole-song request.
                # A noisy singing stem can fail to emit EOS.  The upstream
                # default of 512 then spends many minutes generating garbage;
                # 30-second voice chunks do not need such a large allowance.
                **load_options,
            )
            _prepare_greedy_generation(self._model)
        return _activate_loaded(self._model, "asr")

    def transcribe_neural(self, audio, language, *, context: str = ""):
        chunks = _asr_voice_chunks(audio)
        chunk_windows = tuple(getattr(chunks, "windows", ()))
        line_starts = tuple(
            getattr(
                chunks,
                "line_starts",
                (start for start, _end in chunk_windows),
            )
        )
        self.last_timed_lines = ()
        kwargs = {"audio": chunks if len(chunks) > 1 else chunks[0]}
        normalized_context = " ".join(str(context).split())
        context_key = _context_echo_key(normalized_context)
        if normalized_context:
            kwargs["context"] = (
                [normalized_context] * len(chunks)
                if len(chunks) > 1
                else normalized_context
            )
        if language:
            resolved = resolve_alignment_language("", language)
            kwargs["language"] = [resolved] * len(chunks) if len(chunks) > 1 else resolved
        raw = self._load().transcribe(**kwargs)
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        rows = []
        timed_rows = []
        for index, item in enumerate(items):
            if item is None:
                continue
            value = str(
                item.get("text", "") if isinstance(item, dict) else getattr(item, "text", item)
            ).strip()
            item_rows = [
                line
                for line in value.splitlines()
                if not context_key or _context_echo_key(line) != context_key
            ]
            rows.extend(item_rows)
            chunk_text = "\n".join(item_rows).strip()
            if chunk_text and index < len(line_starts):
                timed_rows.append((float(line_starts[index]), chunk_text))
        if len(items) == len(chunk_windows):
            self.last_timed_lines = tuple(timed_rows)
        text = "\n".join(rows).strip()
        return text, _words(items[0]) if len(items) == 1 else []

    def transcribe(self, audio, language, *, context: str = ""):
        resolved = resolve_alignment_language("", language)
        neural_error: Exception | None = None
        try:
            neural_text, neural_words = self.transcribe_neural(
                audio,
                language,
                context=context,
            )
        except (EngineUnavailableError, OSError, RuntimeError, ValueError) as error:
            neural_error = error
            neural_text, neural_words = "", []
        neural_text = neural_text.strip()
        neural_count = len(tokenize(neural_text))
        if neural_count >= 4 and sum(char.isalnum() for char in neural_text) >= 12:
            print(
                f"[AI] transcription=qwen language={resolved} words={neural_count}",
                flush=True,
            )
            return neural_text, neural_words

        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        direct: list[Word] = []
        direct_text = ""
        if model_path:
            try:
                ctc = self._ctc.setdefault(model_path, _create_ctc_aligner(model_path, resolved))
                direct = ctc.transcribe(audio)
                direct_text = " ".join(word.text for word in direct).strip()
                if len(direct) < 4 or sum(char.isalnum() for char in direct_text) < 12:
                    print(
                        f"[AI] transcription=ctc-direct rejected words={len(direct)}",
                        flush=True,
                    )
                    direct, direct_text = [], ""
            except (
                EngineUnavailableError,
                InvalidArtifactError,
                OSError,
                RuntimeError,
                ValueError,
            ) as error:
                print(
                    f"[AI] transcription=ctc-direct unavailable: {error}",
                    flush=True,
                )
        if direct_text:
            print(
                f"[AI] transcription=ctc-direct language={resolved} words={len(direct)}; "
                f"qwen_words={neural_count}",
                flush=True,
            )
            return direct_text, direct
        if neural_error is not None:
            raise neural_error
        return neural_text, neural_words

    def close(self) -> None:
        for transcriber in self._ctc.values():
            getattr(transcriber, "close", lambda: None)()
        self._ctc.clear()
        self._model = None

    def park(self) -> None:
        for transcriber in self._ctc.values():
            getattr(transcriber, "park", lambda: None)()
        _park_loaded(self._model)


class Qwen3ForcedAligner(IsolatedAlignerMixin, Aligner):
    name = "qwen3-forced-aligner"

    def __init__(self, model: str, *, isolated: bool | None = None):
        self.model_name, self._model, self._ctc = model, None, {}
        self.needs_voice_anchoring = True
        self._configure_isolation(model, model != "test-model" if isolated is None else isolated)

    def _load(self):
        try:
            from qwen_asr import Qwen3ForcedAligner
        except ImportError as error:
            raise EngineUnavailableError("Qwen forced aligner is unavailable") from error
        if self._model is None:
            self._model = _load(Qwen3ForcedAligner, self.model_name, "aligner")
        return _activate_loaded(self._model, "aligner")

    def close(self) -> None:
        self._stop_worker()
        for aligner in self._ctc.values():
            getattr(aligner, "close", lambda: None)()
        self._ctc.clear()
        self._model = None

    def park(self) -> None:
        _park_loaded(self._model)
        for aligner in self._ctc.values():
            getattr(aligner, "park", lambda: None)()

    def transcribe_ctc(self, audio, language=None) -> list[Word]:
        resolved = resolve_alignment_language("", language)
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if not model_path:
            return []
        ctc = self._ctc.setdefault(model_path, _create_ctc_aligner(model_path, resolved))
        return ctc.transcribe(audio)

    def align_ctc_candidates(self, audio, texts, language=None, *, split_seconds=None):
        """Score and align transcript alternatives with one acoustic pass."""
        resolved = resolve_alignment_language("\n".join(texts), language)
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if not model_path:
            raise EngineUnavailableError(f"{resolved} CTC model is unavailable")
        ctc = self._ctc.setdefault(model_path, _create_ctc_aligner(model_path, resolved))
        transcripts = [_ctc_tokens(tokenize(text), resolved) for text in texts]
        return ctc.align_transcripts(audio, transcripts, split_seconds=split_seconds)

    def _raw(self, audio, text, language) -> list[Word]:
        raw = self._load().align(
            audio=str(audio), text=text, language=resolve_alignment_language(text, language)
        )
        item = raw[0] if isinstance(raw, (list, tuple)) and len(raw) == 1 else raw
        return reconcile_words(_words(item), tokenize(text))

    def _ctc_repair(self, words, tokens, samples, rate, span, resolved, runs):
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if not model_path:
            raise EngineUnavailableError(f"{resolved} CTC model is unavailable")
        ctc = self._ctc.setdefault(model_path, _create_ctc_aligner(model_path, resolved))
        for lower, upper in _context_groups(runs, len(words)):
            _, _, crop_start, crop_end, left, right = _repair_bounds(
                words, lower, upper, span, context=0
            )
            crop_start = words[left].end if left is not None else crop_start
            crop_end = words[right].start if right is not None else crop_end
            group_tokens = tokens[lower:upper]
            ctc_tokens = _ctc_tokens(group_tokens, resolved)
            if ctc_tokens != group_tokens:
                print(
                    f"[ctc_repair] normalized tokens[{lower}:{upper}] "
                    f"{group_tokens!r} -> {ctc_tokens!r}",
                    flush=True,
                )
            required = max(1.0, sum(len(token) for token in ctc_tokens) * 0.05)
            if crop_end - crop_start < required:
                deficit = required - (crop_end - crop_start)
                widened_start = max(0.0, crop_start - deficit / 2)
                widened_end = min(span, crop_end + deficit / 2)
                if widened_end - widened_start < required:
                    widened_start = max(0.0, widened_end - required)
                    widened_end = min(span, widened_start + required)
                print(
                    f"[ctc_repair] widening crop for tokens[{lower}:{upper}]={group_tokens!r}: "
                    f"[{crop_start:.3f}..{crop_end:.3f}] ({crop_end - crop_start:.3f}s) is shorter than "
                    f"required {required:.3f}s -> [{widened_start:.3f}..{widened_end:.3f}]",
                    flush=True,
                )
                crop_start, crop_end = widened_start, widened_end
            segment = samples[round(crop_start * rate) : round(crop_end * rate)]
            print(
                f"[ctc_repair] aligning tokens[{lower}:{upper}]={group_tokens!r} "
                f"crop=[{crop_start:.3f}..{crop_end:.3f}] ({crop_end - crop_start:.3f}s, "
                f"{segment.shape[0] if hasattr(segment, 'shape') else len(segment)} samples)",
                flush=True,
            )
            try:
                aligned = ctc.align(segment, rate, ctc_tokens, crop_start)
                words[lower:upper] = _relabel_ctc_words(aligned, group_tokens, lower)
            except (EngineUnavailableError, InvalidArtifactError) as error:
                print(
                    f"[ctc_repair] skipped tokens[{lower}:{upper}]={group_tokens!r}: {error}",
                    flush=True,
                )
        return words

    def _ctc_full(self, samples, rate, tokens, span, resolved):
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if not model_path or not tokens:
            return None
        try:
            ctc = self._ctc.setdefault(model_path, _create_ctc_aligner(model_path, resolved))
            aligned = ctc.align(samples, rate, _ctc_tokens(tokens, resolved), 0)
            words = _relabel_ctc_words(aligned, tokens, 0)
            validated = self._validate(words, tokens, span)
            self.needs_voice_anchoring = False
            print(
                f"[AI] alignment=ctc-full language={resolved} words={len(words)}",
                flush=True,
            )
            return validated
        except (
            EngineUnavailableError,
            InvalidArtifactError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            print(
                f"[AI] alignment=ctc-full unavailable; falling back to Qwen: {error}",
                flush=True,
            )
            return None

    def _ctc_timed_lines(self, samples, rate, tokens, entries, span, resolved):
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if not model_path or not tokens:
            return None

        try:
            ctc = self._ctc.setdefault(model_path, _create_ctc_aligner(model_path, resolved))
            resolved_words: list[Word | None] = [None] * len(tokens)
            for window_start, window_end, lower, upper in _ctc_windows(
                entries,
                span,
                tokens=tokens,
            ):
                segment = samples[round(window_start * rate) : round(window_end * rate)]
                original = tokens[lower:upper]
                encodable = [
                    index
                    for index, token in enumerate(original)
                    if _ctc_language_encodable(token, resolved)
                ]
                for run_start, run_end in _runs(encodable):
                    run_tokens = original[run_start:run_end]
                    try:
                        aligned = ctc.align(
                            segment,
                            rate,
                            _ctc_tokens(run_tokens, resolved),
                            window_start,
                        )
                    except (EngineUnavailableError, InvalidArtifactError):
                        continue
                    relabelled = _relabel_ctc_words(aligned, run_tokens, lower + run_start)
                    resolved_words[lower + run_start : lower + run_end] = relabelled
            _fill_unresolved_timed_lines(resolved_words, entries, tokens, span)
            if any(word is None for word in resolved_words):
                return None
            words = [word for word in resolved_words if word is not None]
            _enforce_monotonic_starts(words, span)
            _repair_collapsed_timed_lines(words, entries, span)
            _repair_timed_line_outliers(words, entries, tokens, span)
            _enforce_monotonic_starts(words, span)
            validated = self._validate(words, tokens, span)
            self.needs_voice_anchoring = False
            print(
                f"[AI] alignment=ctc-timed-lines language={resolved} "
                f"lines={len(entries)} words={len(words)}",
                flush=True,
            )
            return validated
        except (
            EngineUnavailableError,
            InvalidArtifactError,
            OSError,
            RuntimeError,
            ValueError,
        ) as error:
            print(
                f"[AI] alignment=ctc-timed-lines unavailable: {error}",
                flush=True,
            )
            return None

    def _ctc_coarse_text(self, audio, samples, rate, tokens, text, span, resolved):
        from ..word_voicing import voice_activity_intervals

        counts: list[int] = []
        for line in text.splitlines():
            remaining = len(tokenize(line))
            while remaining:
                size = min(24, remaining)
                counts.append(size)
                remaining -= size
        if not counts or sum(counts) != len(tokens):
            counts = [min(24, len(tokens) - offset) for offset in range(0, len(tokens), 24)]
        starts = _coarse_line_starts(
            counts,
            voice_activity_intervals(audio),
            span=span,
        )
        entries, cursor = [], 0
        for start, count in zip(starts, counts, strict=True):
            entries.append((start, cursor, cursor + count))
            cursor += count
        return self._ctc_timed_lines(samples, rate, tokens, entries, span, resolved)

    @staticmethod
    def _validate(words: list[Word], tokens: list[str], span: float) -> list[Word]:
        if len(words) != len(tokens):
            raise InvalidArtifactError(
                f"Aligner returned {len(words)} words for {len(tokens)} tokens"
            )
        if invalid := [
            (index, word.start, word.end)
            for index, word in enumerate(words)
            if _invalid(word, span)
        ]:
            index, start, end = invalid[0]
            raise InvalidArtifactError(
                f"Aligner returned {len(invalid)} invalid timestamps; first is "
                f"token {index} ({tokens[index]!r}) at {start:.3f}..{end:.3f}"
            )
        if disorder := [
            index
            for index in range(1, len(words))
            if words[index].start + 1e-6 < words[index - 1].start
        ]:
            index = disorder[0]
            raise InvalidArtifactError(
                f"Aligner returned {len(disorder)} out-of-order words; token {index} "
                f"({tokens[index]!r}) starts at {words[index].start:.3f} before "
                f"{words[index - 1].start:.3f}"
            )
        return [
            Word(word.start, word.end, token, word.confidence, index)
            for index, (word, token) in enumerate(zip(words, tokens, strict=True))
        ]

    def align(self, audio, text, language):
        return self._validate(self._raw(audio, text, language), tokenize(text), duration(audio))

    def align_timed_lines(self, audio, text, lines, language):
        available = self._ensure_alignment_backend(resolve_alignment_language(text, language))
        if not available:
            return self._align_timed_lines_local(audio, text, lines, language)
        return self._run_isolated("align_timed_lines", audio, text, lines, language)

    def align_chunked_lines(self, audio, text, lines, language):
        available = self._ensure_alignment_backend(resolve_alignment_language(text, language))
        if not available:
            return self._align_chunked_lines_local(audio, text, lines, language)
        return self._run_isolated("align_chunked_lines", audio, text, lines, language)

    def _align_chunked_lines_local(self, audio, text, lines, language):
        return self._align_timed_lines_local(
            audio,
            text,
            lines,
            language,
            adjust_global_offset=False,
        )

    def _align_timed_lines_local(self, audio, text, lines, language, *, adjust_global_offset=True):
        import numpy as np

        from ..audio import read_mono

        span = duration(audio)
        samples, rate = read_mono(audio)
        samples = samples.astype(np.float32)
        offset = _timed_line_offset(samples, rate, lines, span) if adjust_global_offset else 0.0
        if offset:
            lines = tuple(type(line)(line.start + offset, line.text) for line in lines)
            print(f"[AI] timed-lyrics global_offset={offset:+.3f}s", flush=True)
        tokens, entries, groups = _timed_line_plan(text, lines, span)
        resolved = resolve_alignment_language(text, language)
        if ctc_words := self._ctc_timed_lines(samples, rate, tokens, entries, span, resolved):
            return _select_stronger_ctc_alignment(
                ctc_words,
                lambda: self._ctc_full(samples, rate, tokens, span, resolved),
                label="timed lyrics",
            )
        if not self._heavy_alignment_enabled():
            self.needs_voice_anchoring = True
            return self._validate(_interpolate_entries(tokens, entries, span), tokens, span)
        self._ensure_heavy_alignment()
        words: list[Word | None] = [None] * len(tokens)

        def apply(specs):
            specs = [spec for spec in specs if spec[3] - spec[2] >= 0.5]
            if not specs:
                return
            results = self._load().align(
                audio=[
                    (samples[round(start * rate) : round(end * rate)], rate)
                    for _, _, start, end in specs
                ],
                text=[" ".join(tokens[lower:upper]) for lower, upper, *_ in specs],
                language=[resolve_alignment_language(text, language)] * len(specs),
            )
            for (lower, upper, offset, end), result in zip(specs, results, strict=True):
                local = _words(result)
                if len(local) != upper - lower:
                    continue
                for index, word in enumerate(local, start=lower):
                    if words[index] is None and not _invalid(word, end - offset):
                        candidate = Word(
                            word.start + offset,
                            word.end + offset,
                            tokens[index],
                            word.confidence,
                            index,
                        )
                        left = words[index - 1] if index else None
                        right = words[index + 1] if index + 1 < len(words) else None
                        if (left is None or candidate.start + 1e-6 >= left.start) and (
                            right is None or candidate.start <= right.start + 1e-6
                        ):
                            words[index] = candidate

        apply(groups)
        for retry_specs in _timed_line_retry_stages(words, entries, span):
            if any(word is None for word in words):
                apply(retry_specs)
        if any(word is None for word in words):
            variable = {
                "Russian": "KARAOKE_AI_CTC_RU_MODEL",
                "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
            }.get(resolved)
            if variable and (model_path := os.getenv(variable)):
                ctc = self._ctc.setdefault(
                    model_path,
                    _create_ctc_aligner(model_path, resolved),
                )
                for line_index, (start, lower, upper) in enumerate(entries):
                    if not any(word is None for word in words[lower:upper]):
                        continue
                    end = entries[line_index + 1][0] if line_index + 1 < len(entries) else span
                    segment = samples[round(start * rate) : round(end * rate)]
                    try:
                        original_tokens = tokens[lower:upper]
                        aligned = ctc.align(
                            segment, rate, _ctc_tokens(original_tokens, resolved), start
                        )
                        words[lower:upper] = _relabel_ctc_words(aligned, original_tokens, lower)
                    except (EngineUnavailableError, InvalidArtifactError):
                        continue
        for _, lower, upper in entries:
            for index in range(lower + 1, upper - 1):
                if words[index] is not None or words[index - 1] is None or words[index + 1] is None:
                    continue
                start, end = words[index - 1].end, words[index + 1].start
                if end > start:
                    confidence = min(words[index - 1].confidence, words[index + 1].confidence)
                    words[index] = Word(start, end, tokens[index], confidence, index)
        quantum = float(getattr(self._load(), "timestamp_segment_time", 80)) / 1000
        for index in range(len(words) - 1):
            following = words[index + 1]
            if (
                words[index] is not None
                or tokens[index].casefold() not in {"в", "с", "к", "з"}
                or following is None
            ):
                continue
            end = min(following.end, following.start + quantum)
            if index + 2 < len(words) and words[index + 2] is not None:
                end = min(end, words[index + 2].start)
            if end > following.start and following.end > end:
                words[index] = Word(
                    following.start, end, tokens[index], following.confidence, index
                )
                words[index + 1] = Word(
                    end, following.end, tokens[index + 1], following.confidence, index + 1
                )
        _fill_unresolved_timed_lines(words, entries, tokens, span)
        unresolved = [index for index, word in enumerate(words) if word is None]
        if unresolved:
            details = ", ".join(f"{index}:{tokens[index]!r}" for index in unresolved[:12])
            raise InvalidArtifactError(
                f"Timed acoustic alignment failed for {len(unresolved)} words ({details})"
            )
        aligned_words = [word for word in words if word is not None]
        _enforce_monotonic_starts(aligned_words, span)
        _repair_collapsed_timed_lines(aligned_words, entries, span)
        _repair_timed_line_outliers(aligned_words, entries, tokens, span)
        # Independently aligned neighbouring line windows can overlap by one
        # timestamp quantum even when every individual result is valid. Keep
        # that harmless boundary disagreement from rejecting the complete
        # timed-lyrics result and falling back to a slow whole-song pass.
        _enforce_monotonic_starts(aligned_words, span)
        return self._validate(aligned_words, tokens, span)

    def _align_windows(
        self, samples, rate, tokens: list[str], span: float, language: str
    ) -> list[Word]:
        window = 90.0
        # Consecutive windows must overlap by at least half a window,
        # otherwise the strip between their overlap zones is only ever
        # covered by one window with no second candidate to weigh by
        # confidence against -- that gap moves around (not just "the start")
        # depending on how span divides into windows, so it isn't something
        # a single fixed patch can catch; the overlap ratio itself has to be
        # guaranteed for every song length.
        count = max(1, ceil((span - window) / (window / 2)) + 1) if span > window else 1
        starts = [index * max(0, span - window) / max(1, count - 1) for index in range(count)]
        windows = [(start, min(span, start + window)) for start in starts]
        if count > 1:
            # Nothing can precede t=0 or follow t=span, so the very first and
            # very last stretch of the song can never get a second window no
            # matter how much overlap the interior windows have. Add one
            # smaller, tighter window at each end so the intro and outro get
            # a genuine second opinion too.
            windows.append((0.0, min(span, window / 2)))
            windows.append((max(0.0, span - window / 2), span))
        margin = max(8, ceil(len(tokens) * min(30.0, span) / span))
        requests = []
        for start, end in windows:
            lower = max(0, int(start / span * len(tokens)) - margin)
            upper = min(len(tokens), ceil(end / span * len(tokens)) + margin)
            segment = samples[round(start * rate) : round(end * rate)]
            requests.append((lower, upper, start, end, segment))
        results = self._load().align(
            audio=[(segment, rate) for *_, segment in requests],
            text=[" ".join(tokens[lower:upper]) for lower, upper, *_ in requests],
            language=[language] * len(requests),
        )
        candidates: list[list[tuple[float, Word]]] = [[] for _ in tokens]
        for (lower, upper, offset, end, _), result in zip(requests, results, strict=True):
            local = _words(result)
            if len(local) != upper - lower:
                continue
            for index, word in enumerate(local, start=lower):
                if not _invalid(word, end - offset):
                    absolute = Word(
                        word.start + offset,
                        word.end + offset,
                        tokens[index],
                        word.confidence,
                        index,
                    )
                    edge = min(absolute.start - offset, end - absolute.end)
                    candidates[index].append((edge, absolute))
        words, previous = [], 0.0
        for index, options in enumerate(candidates):
            ordered = sorted(
                options,
                key=lambda item: (item[1].confidence, item[0]),
                reverse=True,
            )
            selected = next((word for _, word in ordered if word.start + 1e-6 >= previous), None)
            selected = selected or Word(previous, previous, tokens[index], 0, index)
            words.append(selected)
            previous = selected.start
        return words

    def align_long_text(self, audio, text, language):
        available = self._ensure_alignment_backend(resolve_alignment_language(text, language))
        if not available:
            return self._align_long_text_local(audio, text, language)
        return self._run_isolated("align_long_text", audio, text, language)

    def _safe_coarse_alignment(self, audio, text, tokens, span):
        from ..word_voicing import voice_activity_intervals

        entries = _coarse_entries(text, tokens, voice_activity_intervals(audio), span)
        self.needs_voice_anchoring = False
        return self._validate(_interpolate_entries(tokens, entries, span), tokens, span)

    def _try_ctc_long_alignment(self, audio, text, tokens, span, resolved):
        import numpy as np

        from ..audio import read_mono
        variable = {"Russian": "KARAOKE_AI_CTC_RU_MODEL", "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL"}.get(resolved)
        if not variable or not os.getenv(variable) or not tokens:
            return None
        samples, rate = read_mono(audio)
        samples = samples.astype(np.float32)
        coarse_words = None
        if span > 90.0:
            coarse_words = self._ctc_coarse_text(audio, samples, rate, tokens, text, span, resolved)
        if coarse_words:
            return _select_stronger_ctc_alignment(
                coarse_words,
                lambda: self._ctc_full(samples, rate, tokens, span, resolved),
                label="bounded",
            )
        return self._ctc_full(samples, rate, tokens, span, resolved)

    def _align_long_text_local(self, audio, text, language):
        import numpy as np

        from ..audio import read_mono

        tokens, span = tokenize(text), duration(audio)
        resolved = resolve_alignment_language(text, language)
        samples = rate = None
        self.needs_voice_anchoring = True
        if ctc_words := self._try_ctc_long_alignment(audio, text, tokens, span, resolved):
            return ctc_words
        if not self._heavy_alignment_enabled():
            return self._safe_coarse_alignment(audio, text, tokens, span)
        self._ensure_heavy_alignment()
        # Keep one consistent model call for ordinary songs; stitch windows
        # only when the recording exceeds the model's practical limit.
        if span > WINDOWED_ALIGNMENT_THRESHOLD_SECONDS:
            samples, rate = read_mono(audio)
            samples = samples.astype(np.float32)
            words = self._align_windows(samples, rate, tokens, span, resolved)
        else:
            words = self._raw(audio, text, resolved)
        if len(words) != len(tokens):
            return self._validate(words, tokens, span)
        if samples is None:
            samples, rate = read_mono(audio)
            samples = samples.astype(np.float32)
        previous_invalid = len(words) + 1
        while (runs := _invalid_runs(words, span)) and sum(
            end - start for start, end in runs
        ) < previous_invalid:
            previous_invalid = sum(end - start for start, end in runs)
            repairs = []
            for start, end in runs:
                lower, upper, crop_start, crop_end, *_ = _repair_bounds(words, start, end, span)
                segment = samples[round(crop_start * rate) : round(crop_end * rate)]
                repairs.append((lower, upper, crop_start, crop_end, segment))
            aligned = self._load().align(
                audio=[(segment, rate) for *_, segment in repairs],
                text=[" ".join(tokens[lower:upper]) for lower, upper, *_ in repairs],
                language=[resolved] * len(repairs),
            )
            for (lower, upper, offset, crop_end, _), result in zip(repairs, aligned, strict=True):
                local = _words(result)
                if len(local) != upper - lower:
                    continue
                for index, word in enumerate(local, start=lower):
                    if not _invalid(words[index], span) or _invalid(word, crop_end - offset):
                        continue
                    candidate = Word(
                        word.start + offset,
                        word.end + offset,
                        tokens[index],
                        word.confidence,
                        index,
                    )
                    left = words[index - 1] if index else None
                    right = words[index + 1] if index + 1 < len(words) else None
                    if (left is None or candidate.start >= left.start) and (
                        right is None or _invalid(right, span) or candidate.start <= right.start
                    ):
                        words[index] = candidate
        structural = _invalid_runs(words, span)
        try:
            if structural:
                try:
                    self._ctc_repair(words, tokens, samples, rate, span, resolved, structural)
                except (EngineUnavailableError, InvalidArtifactError) as error:
                    print(
                        f"[ctc_repair] unavailable, using local interpolation: {error}", flush=True
                    )
                if _invalid_runs(words, span):
                    _interpolate_invalid_words(words, tokens, span)
                _enforce_monotonic_starts(words, span)
            validated = self._validate(words, tokens, span)
        except (EngineUnavailableError, InvalidArtifactError) as error:
            raise InvalidArtifactError(f"Qwen acoustic alignment failed: {error}") from error
        suspicious = _acoustic_runs(validated, samples, rate)
        if not suspicious:
            return validated
        try:
            repaired = self._ctc_repair(
                validated.copy(), tokens, samples, rate, span, resolved, suspicious
            )
            _enforce_monotonic_starts(repaired, span)
            return self._validate(repaired, tokens, span)
        except (EngineUnavailableError, InvalidArtifactError):
            return validated


class UniformTextFallback(Transcriber, Aligner):
    name = "uniform-fallback"

    def align(self, audio, text, language):
        tokens, span = tokenize(text), duration(audio)
        return (
            [
                Word(index * span / len(tokens), (index + 1) * span / len(tokens), token, 0, index)
                for index, token in enumerate(tokens)
            ]
            if tokens
            else []
        )

    def align_long_text(self, audio, text, language):
        return self.align(audio, text, language)

    def transcribe(self, audio, language, *, context: str = ""):
        return "", []
