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
from .base import Aligner, Transcriber
from .device import select_torch_device
from .timed_line_repairs import repair_timed_line_outliers as _repair_timed_line_outliers

ASR_PIPELINE_VERSION = "clean-v1"
LONG_TEXT_ALIGNMENT_VERSION = "clean-v2"
LANGUAGES = {"en": "English", "ru": "Russian", "uk": "Ukrainian"}
WINDOWED_ALIGNMENT_THRESHOLD_SECONDS = 600.0
_NUMBER_RE = re.compile(r"\d+")
_NUMBER_CONFIG = {
    "Russian": {
        "small": (
            "ноль", "один", "два", "три", "четыре", "пять", "шесть",
            "семь", "восемь", "девять", "десять", "одиннадцать",
            "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
            "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать",
        ),
        "tens": ("", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"),
        "hundreds": ("", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"),
        "feminine": {1: "одна", 2: "две"},
        "scales": (
            (10**12, ("триллион", "триллиона", "триллионов"), False),
            (10**9, ("миллиард", "миллиарда", "миллиардов"), False),
            (10**6, ("миллион", "миллиона", "миллионов"), False),
            (10**3, ("тысяча", "тысячи", "тысяч"), True),
        ),
    },
    "Ukrainian": {
        "small": (
            "нуль", "один", "два", "три", "чотири", "п'ять", "шість",
            "сім", "вісім", "дев'ять", "десять", "одинадцять",
            "дванадцять", "тринадцять", "чотирнадцять", "п'ятнадцять",
            "шістнадцять", "сімнадцять", "вісімнадцять", "дев'ятнадцять",
        ),
        "tens": ("", "", "двадцять", "тридцять", "сорок", "п'ятдесят", "шістдесят", "сімдесят", "вісімдесят", "дев'яносто"),
        "hundreds": ("", "сто", "двісті", "триста", "чотириста", "п'ятсот", "шістсот", "сімсот", "вісімсот", "дев'ятсот"),
        "feminine": {1: "одна", 2: "дві"},
        "scales": (
            (10**12, ("трильйон", "трильйони", "трильйонів"), False),
            (10**9, ("мільярд", "мільярди", "мільярдів"), False),
            (10**6, ("мільйон", "мільйони", "мільйонів"), False),
            (10**3, ("тисяча", "тисячі", "тисяч"), True),
        ),
    },
    "English": {
        "small": (
            "zero", "one", "two", "three", "four", "five", "six", "seven",
            "eight", "nine", "ten", "eleven", "twelve", "thirteen",
            "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
            "nineteen",
        ),
        "tens": ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"),
        "scales": ((10**12, "trillion"), (10**9, "billion"), (10**6, "million"), (10**3, "thousand")),
    },
}


def _plural_form(value: int, forms: tuple[str, str, str]) -> str:
    last_two = value % 100
    if 11 <= last_two <= 14:
        return forms[2]
    last = value % 10
    return forms[0] if last == 1 else forms[1] if 2 <= last <= 4 else forms[2]


def _triplet_words(value: int, language: str, feminine: bool = False) -> list[str]:
    config = _NUMBER_CONFIG[language]
    if language == "English":
        result = []
        if value >= 100:
            result.extend((config["small"][value // 100], "hundred"))
            value %= 100
        if value:
            if value < 20:
                result.append(config["small"][value])
            else:
                result.append(config["tens"][value // 10])
                if value % 10:
                    result.append(config["small"][value % 10])
        return result

    result = []
    if value >= 100:
        result.append(config["hundreds"][value // 100])
        value %= 100
    if not value:
        return result
    if value < 20:
        result.append(config["feminine"].get(
            value, config["small"][value]) if feminine else config["small"][value])
        return result
    result.append(config["tens"][value // 10])
    unit = value % 10
    if unit:
        result.append(config["feminine"].get(
            unit, config["small"][unit]) if feminine else config["small"][unit])
    return result


def _integer_words(value: int, language: str) -> str:
    language = language if language in _NUMBER_CONFIG else "English"
    config = _NUMBER_CONFIG[language]
    if value == 0:
        return config["small"][0]
    if value >= 10**15:
        return "".join(config["small"][int(digit)] for digit in str(value))

    result = []
    if language == "English":
        for scale, name in config["scales"]:
            group, value = divmod(value, scale)
            if group:
                result.extend(_triplet_words(group, language))
                result.append(name)
    else:
        for scale, forms, feminine in config["scales"]:
            group, value = divmod(value, scale)
            if group:
                result.extend(_triplet_words(group, language, feminine))
                result.append(_plural_form(group, forms))
    result.extend(_triplet_words(value, language))
    return "".join(result)


def _ctc_token(token: str, language: str) -> str:
    """Convert digits to pronounceable letters without changing canonical lyrics."""
    normalized = unicodedata.normalize("NFKC", token).casefold()

    def replace(match: re.Match[str]) -> str:
        digits = match.group(0)
        if len(digits) > 1 and digits.startswith("0"):
            return "".join(_integer_words(int(digit), language) for digit in digits)
        return _integer_words(int(digits), language)

    normalized = _NUMBER_RE.sub(replace, normalized)
    return "".join(char for char in normalized if char == "'" or unicodedata.category(char)[:1] == "L")


def _ctc_tokens(tokens: list[str], language: str) -> list[str]:
    return [_ctc_token(token, language) or token for token in tokens]


def _ctc_language_encodable(token: str, language: str) -> bool:
    normalized = _ctc_token(token, language)
    if language not in {"Russian", "Ukrainian"}:
        return bool(normalized)
    return bool(normalized) and all(
        character == "'" or "CYRILLIC" in unicodedata.name(character, "")
        for character in normalized
    )


def _ctc_role(language: str) -> str:
    return "ctc_uk" if language == "Ukrainian" else "ctc_ru"


def _create_ctc_aligner(model_path: str, language: str):
    from .ctc import CTCWordAligner

    aligner = CTCWordAligner(model_path)
    aligner.role = _ctc_role(language)
    return aligner


def _relabel_ctc_words(words: list[Word], tokens: list[str], offset: int = 0) -> list[Word]:
    if len(words) != len(tokens):
        raise InvalidArtifactError(
            f"CTC returned {len(words)} words for {len(tokens)} tokens")
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
                words[index] = Word(anchor, fallback_end,
                                    tokens[index], 0.0, index)
            continue

        weights = [max(1, sum(char.isalnum() for char in tokens[index]))
                   for index in range(start, end)]
        total, consumed, cursor = sum(weights), 0, lower
        for index, weight in zip(range(start, end), weights, strict=True):
            consumed += weight
            boundary = upper if index == end - 1 else lower + \
                (upper - lower) * consumed / total
            words[index] = Word(cursor, boundary, tokens[index], 0.0, index)
            cursor = boundary
    return words


_LATIN_CYRILLIC_HOMOGLYPHS = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
    "y": "у",
})
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
        has_cyrillic = any("а" <= char.casefold() <= "я" or char.casefold() in "ёіїєґ"
                            for char in token)
        has_latin = any("a" <= char.casefold() <= "z" for char in token)
        if has_cyrillic and has_latin:
            token = token.translate(_LATIN_CYRILLIC_HOMOGLYPHS)
        if _SUNG_VOCALISATION_RE.fullmatch(token):
            return re.sub(r"[\-‐‑‒–—]", " ", token)
        return _REPEATED_HYPHEN_WORD_RE.sub(r"\1 \1", token)

    return re.sub(r"\S+", clean_token, text)


def tokenize(text: str) -> list[str]:
    def kept(char: str) -> bool:
        return char == "'" or unicodedata.category(char)[:1] in {"L", "N"}

    result = []
    for part in normalize_lyrics_text(text).split():
        positions = [index for index, char in enumerate(part) if kept(char)]
        if positions:
            result.append(part[positions[0]:positions[-1] + 1])
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
        data = item if isinstance(item, dict) else vars(item) if hasattr(item, "__dict__") else {
            "text": getattr(item, "text", ""),
            "start_time": getattr(item, "start_time", None),
            "end_time": getattr(item, "end_time", None),
        }
        text = str(data.get("text") or data.get("word") or "").strip()
        try:
            result.append(Word(float(data.get("start", data.get("start_time"))), float(data.get(
                "end", data.get("end_time"))), text, float(data.get("confidence", 1)), index))
        except (TypeError, ValueError):
            continue
    return result


def _invalid(word: Word, span: float) -> bool:
    return word.start < 0 or word.end <= word.start or word.end > span + 0.1


def _invalid_runs(words: list[Word], span: float) -> list[tuple[int, int]]:
    indices = [index for index, word in enumerate(
        words) if _invalid(word, span)]
    runs: list[list[int]] = []
    for index in indices:
        if not runs or index != runs[-1][1]:
            runs.append([index, index + 1])
        else:
            runs[-1][1] = index + 1
    return [(start, end) for start, end in runs]


def _runs(indices: list[int]) -> list[tuple[int, int]]:
    result: list[list[int]] = []
    for index in sorted(set(indices)):
        if not result or index != result[-1][1]:
            result.append([index, index + 1])
        else:
            result[-1][1] = index + 1
    return [(start, end) for start, end in result]


def _longest_false_run(mask) -> int:
    """Length of the longest run of consecutive False values in mask."""
    import numpy as np

    if not len(mask):
        return 0
    padded = np.concatenate(([True], mask, [True]))
    edges = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(edges == -1)
    if not len(starts):
        return 0
    ends = np.flatnonzero(edges == 1)
    return int(np.max(ends - starts))


def _acoustic_runs(words: list[Word], samples, rate: int) -> list[tuple[int, int]]:
    import numpy as np

    bad = {
        index
        for index in range(len(words) - 1)
        if words[index].end > words[index + 1].start + 0.04
    }
    bad.update(index + 1 for index in tuple(bad))
    mono = np.asarray(samples, dtype=np.float32)
    if mono.ndim > 1:
        mono = mono.mean(axis=1)
    frame = max(1, round(rate * 0.02))
    usable = len(mono) // frame * frame
    if usable < frame:
        return _runs(list(bad))
    rms = np.sqrt(np.mean(mono[:usable].reshape(-1, frame) ** 2, axis=1))
    audible = rms[rms > 1e-7]
    if not len(audible):
        return _runs(list(bad))
    floor, signal = float(np.percentile(audible, 15)), float(
        np.percentile(audible, 85))
    threshold = max(min(floor * 2.5, signal * 0.25), signal * 0.025)
    active = rms >= threshold
    for index, word in enumerate(words):
        word_duration = word.end - word.start
        if word_duration < 0.45:
            continue
        lower = max(0, round(word.start * rate / frame))
        upper = min(len(active), max(
            lower + 1, round(word.end * rate / frame)))
        longest = _longest_false_run(active[lower:upper])
        silent_duration = longest * frame / rate
        if silent_duration >= min(0.55, word_duration * 0.45):
            bad.add(index)
    return _runs(list(bad))


def _context_groups(runs: list[tuple[int, int]], count: int) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    for start, end in runs:
        lower, upper = max(0, start - 2), min(count, end + 2)
        if groups and lower <= groups[-1][1]:
            groups[-1] = groups[-1][0], max(groups[-1][1], upper)
        else:
            groups.append((lower, upper))
    return groups


def _repair_bounds(words: list[Word], start: int, end: int, span: float, context: int = 2):
    left = next(
        (index for index in range(start - 1, -1, -1)
         if not _invalid(words[index], span)),
        None,
    )
    right = next(
        (index for index in range(end, len(words))
         if not _invalid(words[index], span)),
        None,
    )
    if left is None and right is None:
        return 0, len(words), 0.0, span, left, right
    estimate = max(4.0, (end - start) * 1.25)
    lower, upper = (
        max(0, start - (context if left is not None else 0)),
        min(len(words), end + (context if right is not None else 0)),
    )
    if left is not None and right is not None:
        crop_start = min(words[left].start, words[right].start) - 1
        crop_end = max(words[left].end, words[right].end) + 1
    elif left is not None:
        crop_start, crop_end = words[left].start - \
            1, words[left].end + estimate
    else:
        crop_start, crop_end = words[right].start - \
            estimate, words[right].end + 1
    crop_start, crop_end = max(0, crop_start), min(span, crop_end)
    if crop_end - crop_start < 1.0:
        crop_start, crop_end = max(
            0.0, crop_end - 1.0), min(span, crop_start + 1.0)
    return lower, upper, crop_start, crop_end, left, right


def _enforce_monotonic_starts(words: list[Word], span: float) -> None:
    """Clamp any word whose start regressed behind its predecessor's start.

    Repair passes (notably CTC repair) rewrite a sub-range in isolation and
    only check consistency against words immediately adjacent to that range,
    not the full downstream chain, so a later word's start can end up before
    an earlier word's start. `_validate` requires non-decreasing starts, so
    fix that up front instead of failing the whole song over it.
    """
    for index in range(1, len(words)):
        if words[index].start + 1e-6 < words[index - 1].start:
            new_start = min(words[index - 1].start, span)
            new_end = words[index].end if words[index].end > new_start else min(
                span, new_start + 0.05)
            words[index] = Word(new_start, new_end, words[index].text,
                                words[index].confidence, words[index].index)


def _repair_collapsed_timed_lines(
    words: list[Word], entries: list[tuple[float, int, int]], span: float
) -> None:
    """Expand an implausibly collapsed multi-word line inside its trusted LRC window."""
    for line_index, (line_start, lower, upper) in enumerate(entries):
        if upper - lower < 3:
            continue
        line_end = entries[line_index + 1][0] if line_index + 1 < len(entries) else span
        window_start, window_end = max(0.0, line_start), min(span, line_end)
        window_span = window_end - window_start
        if window_span < 1.0:
            continue
        group = words[lower:upper]
        measured_span = max(word.end for word in group) - min(word.start for word in group)
        if measured_span >= min(1.0, window_span * 0.35):
            continue
        weights = [max(1, sum(char.isalnum() for char in word.text)) for word in group]
        cursor = window_start
        for index, (word, weight) in enumerate(zip(group, weights, strict=True), start=lower):
            boundary = (
                window_end
                if index == upper - 1
                else cursor + (window_end - cursor) * weight / sum(weights[index - lower:])
            )
            words[index] = Word(cursor, boundary, word.text, 0.0, word.index)
            cursor = boundary


def _fill_unresolved_timed_lines(
    words: list[Word | None],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    """Recover only failed LRC lines instead of rejecting the complete alignment."""
    for line_index, (line_start, lower, upper) in enumerate(entries):
        if all(word is not None for word in words[lower:upper]):
            continue
        line_end = entries[line_index + 1][0] if line_index + 1 < len(entries) else span
        window_start, window_end = max(0.0, line_start), min(span, line_end)
        if window_end <= window_start:
            continue
        index = lower
        while index < upper:
            if words[index] is not None:
                index += 1
                continue
            run_start = index
            while index < upper and words[index] is None:
                index += 1
            run_end = index
            left_word = words[run_start - 1] if run_start > lower else None
            right_word = words[run_end] if run_end < upper else None
            start = max(window_start, left_word.end if left_word is not None else window_start)
            end = min(window_end, right_word.start if right_word is not None else window_end)
            if end <= start:
                # The acoustic neighbours leave no legal gap. Rebuild this one
                # line from its trusted timestamp instead of disturbing any
                # other successfully aligned line.
                run_start, run_end, start, end = lower, upper, window_start, window_end
            weights = [
                max(1, sum(char.isalnum() for char in token))
                for token in tokens[run_start:run_end]
            ]
            remaining = sum(weights)
            cursor = start
            for word_index, weight in zip(range(run_start, run_end), weights, strict=True):
                boundary = (
                    end
                    if word_index == run_end - 1
                    else cursor + (end - cursor) * weight / remaining
                )
                words[word_index] = Word(
                    cursor, boundary, tokens[word_index], 0.0, word_index
                )
                cursor = boundary
                remaining -= weight


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


def _timed_line_retry_stages(words, entries, span: float):
    """Yield retry policies in order, observing words filled by earlier stages."""
    per_line = []
    for index, (start, lower, upper) in enumerate(entries):
        if any(word is None for word in words[lower:upper]):
            end = entries[index + 1][0] if index + 1 < len(entries) else span
            per_line.append((lower, upper, max(0, start - 0.5), min(span, end + 0.5)))
    yield per_line

    contexts = []
    for index, (_, lower, upper) in enumerate(entries):
        if any(word is None for word in words[lower:upper]):
            first, last = max(0, index - 1), min(len(entries), index + 2)
            start = max(0, entries[first][0] - 1)
            end = min(span, (entries[last][0] if last < len(entries) else span) + 1)
            contexts.append((entries[first][1], entries[last - 1][2], start, end))
    yield contexts

    triads = []
    for _, lower, upper in entries:
        for index in range(lower + 1, upper - 1):
            if words[index] is None and words[index - 1] is not None and words[index + 1] is not None:
                start = max(0, words[index - 1].start - 0.5)
                end = min(span, words[index + 1].end + 0.5)
                triads.append((index - 1, index + 2, start, end))
    yield triads

    singles = []
    for _, lower, upper in entries:
        for index in range(lower + 1, upper - 1):
            if words[index] is None and words[index - 1] is not None and words[index + 1] is not None:
                center = (words[index - 1].end + words[index + 1].start) / 2
                start = max(words[index - 1].start, center - 1)
                end = min(words[index + 1].end, center + 1)
                if end > start:
                    singles.append((index, index + 1, start, end))
    yield singles

    wide_singles = []
    for line_index, (line_start, lower, upper) in enumerate(entries):
        line_end = entries[line_index + 1][0] if line_index + 1 < len(entries) else span
        for index in range(lower, upper):
            if words[index] is None:
                wide_singles.append(
                    (index, index + 1, max(0, line_start - 0.75), min(span, line_end + 0.75))
                )
    yield wide_singles


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


def _asr_voice_chunks(audio, max_seconds: float = 30.0):
    """Batch long vocals at real silence boundaries instead of one huge prompt."""
    import numpy as np

    from ..audio import read_mono
    from ..word_voicing import voice_activity_intervals

    samples, rate = read_mono(audio)
    intervals = voice_activity_intervals(audio)
    if not intervals or len(samples) <= round(max_seconds * rate):
        return [(samples.astype(np.float32), rate)]

    windows = []
    first, previous_end = intervals[0]
    for start, end in intervals[1:]:
        if end - first > max_seconds:
            windows.append((first, previous_end))
            first = start
        previous_end = end
    windows.append((first, previous_end))

    chunks = []
    for start, end in windows:
        lower = max(0, round((start - 0.12) * rate))
        upper = min(len(samples), round((end + 0.12) * rate))
        while upper - lower > round(max_seconds * rate):
            split = lower + round(max_seconds * rate)
            chunks.append((samples[lower:split].astype(np.float32), rate))
            lower = split
        if upper > lower:
            chunks.append((samples[lower:upper].astype(np.float32), rate))
    return chunks or [(samples.astype(np.float32), rate)]


class Qwen3Transcriber(Transcriber):
    name = "qwen3-asr"

    def __init__(self, model: str):
        self.model_name, self._model, self._ctc = model, None, {}

    def _load(self):
        try:
            from qwen_asr import Qwen3ASRModel
        except ImportError as error:
            raise EngineUnavailableError("qwen-asr is unavailable") from error
        if self._model is None:
            self._model = _load(
                Qwen3ASRModel,
                self.model_name,
                "asr",
                # Two concurrent chunks keep the GPU responsive for the
                # desktop compositor/browser while retaining most of the
                # throughput benefit over one whole-song request.
                max_inference_batch_size=2,
                # A noisy singing stem can fail to emit EOS.  The upstream
                # default of 512 then spends many minutes generating garbage;
                # 30-second voice chunks do not need such a large allowance.
                max_new_tokens=192,
            )
        return _activate_loaded(self._model, "asr")

    def transcribe_neural(self, audio, language):
        chunks = _asr_voice_chunks(audio)
        kwargs = {"audio": chunks if len(chunks) > 1 else chunks[0]}
        if language:
            resolved = resolve_alignment_language("", language)
            kwargs["language"] = [resolved] * len(chunks) if len(chunks) > 1 else resolved
        raw = self._load().transcribe(**kwargs)
        items = list(raw) if isinstance(raw, (list, tuple)) else [raw]
        text = "\n".join(
            str(item.get("text", "") if isinstance(item, dict)
                else getattr(item, "text", item)).strip()
            for item in items
            if item is not None
        ).strip()
        return text, _words(items[0]) if len(items) == 1 else []

    def transcribe(self, audio, language):
        resolved = resolve_alignment_language("", language)
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if model_path:
            try:
                ctc = self._ctc.setdefault(model_path, _create_ctc_aligner(model_path, resolved))
                direct = ctc.transcribe(audio)
                text = " ".join(word.text for word in direct).strip()
                if len(direct) >= 4 and sum(char.isalnum() for char in text) >= 12:
                    print(
                        f"[AI] transcription=ctc-direct language={resolved} "
                        f"words={len(direct)}",
                        flush=True,
                    )
                    return text, direct
                print(
                    f"[AI] transcription=ctc-direct rejected words={len(direct)}; "
                    "falling back to Qwen",
                    flush=True,
                )
            except (EngineUnavailableError, InvalidArtifactError, OSError, RuntimeError, ValueError) as error:
                print(
                    f"[AI] transcription=ctc-direct unavailable; falling back to Qwen: {error}",
                    flush=True,
                )
        return self.transcribe_neural(audio, language)

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
            raise EngineUnavailableError(
                "Qwen forced aligner is unavailable") from error
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
        ctc = self._ctc.setdefault(
            model_path, _create_ctc_aligner(model_path, resolved)
        )
        return ctc.transcribe(audio)

    def align_ctc_candidates(
        self, audio, texts, language=None, *, split_seconds=None
    ):
        """Score and align transcript alternatives with one acoustic pass."""
        resolved = resolve_alignment_language("\n".join(texts), language)
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if not model_path:
            raise EngineUnavailableError(
                f"{resolved} CTC model is unavailable"
            )
        ctc = self._ctc.setdefault(
            model_path, _create_ctc_aligner(model_path, resolved)
        )
        transcripts = [
            _ctc_tokens(tokenize(text), resolved)
            for text in texts
        ]
        return ctc.align_transcripts(
            audio, transcripts, split_seconds=split_seconds
        )

    def _raw(self, audio, text, language) -> list[Word]:
        raw = self._load().align(audio=str(audio), text=text,
                                 language=resolve_alignment_language(text, language))
        item = raw[0] if isinstance(
            raw, (list, tuple)) and len(raw) == 1 else raw
        return reconcile_words(_words(item), tokenize(text))

    def _ctc_repair(self, words, tokens, samples, rate, span, resolved, runs):
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        model_path = os.getenv(variable) if variable else None
        if not model_path:
            raise EngineUnavailableError(
                f"{resolved} CTC model is unavailable")
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
            segment = samples[round(crop_start * rate):round(crop_end * rate)]
            print(
                f"[ctc_repair] aligning tokens[{lower}:{upper}]={group_tokens!r} "
                f"crop=[{crop_start:.3f}..{crop_end:.3f}] ({crop_end - crop_start:.3f}s, "
                f"{segment.shape[0] if hasattr(segment, 'shape') else len(segment)} samples)",
                flush=True,
            )
            try:
                aligned = ctc.align(segment, rate, ctc_tokens, crop_start)
                words[lower:upper] = _relabel_ctc_words(
                    aligned, group_tokens, lower)
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
            ctc = self._ctc.setdefault(
                model_path, _create_ctc_aligner(model_path, resolved)
            )
            resolved_words: list[Word | None] = [None] * len(tokens)
            for line_index, (line_start, lower, upper) in enumerate(entries):
                next_start = (
                    entries[line_index + 1][0]
                    if line_index + 1 < len(entries)
                    else span
                )
                window_start = max(0.0, line_start - 0.6)
                window_end = min(
                    span,
                    max(line_start + 0.75, min(next_start + 0.75, line_start + 20.0)),
                )
                segment = samples[
                    round(window_start * rate):round(window_end * rate)
                ]
                original = tokens[lower:upper]
                encodable = [
                    index for index, token in enumerate(original)
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
                    relabelled = _relabel_ctc_words(
                        aligned, run_tokens, lower + run_start
                    )
                    resolved_words[
                        lower + run_start:lower + run_end
                    ] = relabelled
            _fill_unresolved_timed_lines(
                resolved_words, entries, tokens, span
            )
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
    @staticmethod
    def _validate(words: list[Word], tokens: list[str], span: float) -> list[Word]:
        if len(words) != len(tokens):
            raise InvalidArtifactError(
                f"Aligner returned {len(words)} words for {len(tokens)} tokens")
        if invalid := [(index, word.start, word.end) for index, word in enumerate(words) if _invalid(word, span)]:
            index, start, end = invalid[0]
            raise InvalidArtifactError(
                f"Aligner returned {len(invalid)} invalid timestamps; first is "
                f"token {index} ({tokens[index]!r}) at {start:.3f}..{end:.3f}"
            )
        if disorder := [
            index for index in range(1, len(words))
            if words[index].start + 1e-6 < words[index - 1].start
        ]:
            index = disorder[0]
            raise InvalidArtifactError(
                f"Aligner returned {len(disorder)} out-of-order words; token {index} "
                f"({tokens[index]!r}) starts at {words[index].start:.3f} before "
                f"{words[index - 1].start:.3f}"
            )
        return [Word(word.start, word.end, token, word.confidence, index) for index, (word, token) in enumerate(zip(words, tokens, strict=True))]

    def align(self, audio, text, language):
        return self._validate(self._raw(audio, text, language), tokenize(text), duration(audio))

    def align_timed_lines(self, audio, text, lines, language):
        self._ensure_alignment_backend(resolve_alignment_language(text, language))
        return self._run_isolated("align_timed_lines", audio, text, lines, language)

    def _align_timed_lines_local(self, audio, text, lines, language):
        import numpy as np

        from ..audio import read_mono

        span = duration(audio)
        samples, rate = read_mono(audio)
        samples = samples.astype(np.float32)
        offset = _timed_line_offset(samples, rate, lines, span)
        if offset:
            lines = tuple(type(line)(line.start + offset, line.text) for line in lines)
            print(f"[AI] timed-lyrics global_offset={offset:+.3f}s", flush=True)
        tokens, entries, groups = _timed_line_plan(text, lines, span)
        resolved = resolve_alignment_language(text, language)
        if ctc_words := self._ctc_timed_lines(
            samples, rate, tokens, entries, span, resolved
        ):
            return ctc_words
        self._ensure_heavy_alignment()
        words: list[Word | None] = [None] * len(tokens)

        def apply(specs):
            specs = [spec for spec in specs if spec[3] - spec[2] >= 0.5]
            if not specs:
                return
            results = self._load().align(
                audio=[(samples[round(start * rate):round(end * rate)], rate)
                       for _, _, start, end in specs],
                text=[" ".join(tokens[lower:upper])
                      for lower, upper, *_ in specs],
                language=[resolve_alignment_language(
                    text, language)] * len(specs),
            )
            for (lower, upper, offset, end), result in zip(specs, results, strict=True):
                local = _words(result)
                if len(local) != upper - lower:
                    continue
                for index, word in enumerate(local, start=lower):
                    if words[index] is None and not _invalid(word, end - offset):
                        candidate = Word(
                            word.start + offset, word.end + offset,
                            tokens[index], word.confidence, index,
                        )
                        left = words[index - 1] if index else None
                        right = words[index + 1] if index + \
                            1 < len(words) else None
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
                    end = entries[line_index + 1][0] if line_index + \
                        1 < len(entries) else span
                    segment = samples[round(start * rate):round(end * rate)]
                    try:
                        original_tokens = tokens[lower:upper]
                        aligned = ctc.align(segment, rate, _ctc_tokens(
                            original_tokens, resolved), start)
                        words[lower:upper] = _relabel_ctc_words(
                            aligned, original_tokens, lower)
                    except (EngineUnavailableError, InvalidArtifactError):
                        continue
        for _, lower, upper in entries:
            for index in range(lower + 1, upper - 1):
                if words[index] is not None or words[index - 1] is None or words[index + 1] is None:
                    continue
                start, end = words[index - 1].end, words[index + 1].start
                if end > start:
                    confidence = min(
                        words[index - 1].confidence, words[index + 1].confidence)
                    words[index] = Word(
                        start, end, tokens[index], confidence, index)
        quantum = float(
            getattr(self._load(), "timestamp_segment_time", 80)) / 1000
        for index in range(len(words) - 1):
            following = words[index + 1]
            if words[index] is not None or tokens[index].casefold() not in {"в", "с", "к", "з"} or following is None:
                continue
            end = min(following.end, following.start + quantum)
            if index + 2 < len(words) and words[index + 2] is not None:
                end = min(end, words[index + 2].start)
            if end > following.start and following.end > end:
                words[index] = Word(following.start, end,
                                    tokens[index], following.confidence, index)
                words[index + 1] = Word(
                    end, following.end, tokens[index +
                                               1], following.confidence, index + 1
                )
        _fill_unresolved_timed_lines(words, entries, tokens, span)
        unresolved = [index for index,
                      word in enumerate(words) if word is None]
        if unresolved:
            details = ", ".join(
                f"{index}:{tokens[index]!r}" for index in unresolved[:12])
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

    def _align_windows(self, samples, rate, tokens: list[str], span: float, language: str) -> list[Word]:
        window = 90.0
        # Consecutive windows must overlap by at least half a window,
        # otherwise the strip between their overlap zones is only ever
        # covered by one window with no second candidate to weigh by
        # confidence against -- that gap moves around (not just "the start")
        # depending on how span divides into windows, so it isn't something
        # a single fixed patch can catch; the overlap ratio itself has to be
        # guaranteed for every song length.
        count = max(1, ceil((span - window) / (window / 2)) +
                    1) if span > window else 1
        starts = [index * max(0, span - window) / max(1, count - 1)
                  for index in range(count)]
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
            segment = samples[round(start * rate):round(end * rate)]
            requests.append((lower, upper, start, end, segment))
        results = self._load().align(
            audio=[(segment, rate) for *_, segment in requests],
            text=[" ".join(tokens[lower:upper])
                  for lower, upper, *_ in requests],
            language=[language] * len(requests),
        )
        candidates: list[list[tuple[float, Word]]] = [[] for _ in tokens]
        for (lower, upper, offset, end, _), result in zip(requests, results, strict=True):
            local = _words(result)
            if len(local) != upper - lower:
                continue
            for index, word in enumerate(local, start=lower):
                if not _invalid(word, end - offset):
                    absolute = Word(word.start + offset, word.end +
                                    offset, tokens[index], word.confidence, index)
                    edge = min(absolute.start - offset, end - absolute.end)
                    candidates[index].append((edge, absolute))
        words, previous = [], 0.0
        for index, options in enumerate(candidates):
            ordered = sorted(
                options,
                key=lambda item: (item[1].confidence, item[0]),
                reverse=True,
            )
            selected = next(
                (word for _, word in ordered if word.start + 1e-6 >= previous), None)
            selected = selected or Word(
                previous, previous, tokens[index], 0, index)
            words.append(selected)
            previous = selected.start
        return words

    def align_long_text(self, audio, text, language):
        self._ensure_alignment_backend(resolve_alignment_language(text, language))
        return self._run_isolated("align_long_text", audio, text, language)

    def _align_long_text_local(self, audio, text, language):
        import numpy as np

        from ..audio import read_mono

        tokens, span = tokenize(text), duration(audio)
        resolved = resolve_alignment_language(text, language)
        samples = rate = None
        self.needs_voice_anchoring = True
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        if variable and os.getenv(variable) and tokens:
            samples, rate = read_mono(audio)
            samples = samples.astype(np.float32)
            if ctc_words := self._ctc_full(samples, rate, tokens, span, resolved):
                return ctc_words
        self._ensure_heavy_alignment()
        # Windowing is a necessary fallback for the model's practical limit,
        # necessary evil that trades one long, consistent alignment for
        # several short ones stitched together at boundaries that are never
        # perfectly seamless (as every attempt to patch the seam coverage
        # has shown: fixing one boundary just moves the artifact to the
        # next one). So the single-call path should cover every length the
        # model can actually take in one shot, and windowing should only be
        # the fallback for genuinely long outliers, not the default path
        # for an ordinary song.
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
        while (runs := _invalid_runs(words, span)) and sum(end - start for start, end in runs) < previous_invalid:
            previous_invalid = sum(end - start for start, end in runs)
            repairs = []
            for start, end in runs:
                lower, upper, crop_start, crop_end, * \
                    _ = _repair_bounds(words, start, end, span)
                segment = samples[round(crop_start * rate)
                                        : round(crop_end * rate)]
                repairs.append((lower, upper, crop_start, crop_end, segment))
            aligned = self._load().align(
                audio=[(segment, rate) for *_, segment in repairs],
                text=[" ".join(tokens[lower:upper])
                      for lower, upper, *_ in repairs],
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
                    right = words[index + 1] if index + \
                        1 < len(words) else None
                    if (left is None or candidate.start >= left.start) and (right is None or _invalid(right, span) or candidate.start <= right.start):
                        words[index] = candidate
        structural = _invalid_runs(words, span)
        try:
            if structural:
                try:
                    self._ctc_repair(words, tokens, samples,
                                     rate, span, resolved, structural)
                except (EngineUnavailableError, InvalidArtifactError) as error:
                    print(
                        f"[ctc_repair] unavailable, using local interpolation: {error}", flush=True)
                if _invalid_runs(words, span):
                    _interpolate_invalid_words(words, tokens, span)
                _enforce_monotonic_starts(words, span)
            validated = self._validate(words, tokens, span)
        except (EngineUnavailableError, InvalidArtifactError) as error:
            raise InvalidArtifactError(
                f"Qwen acoustic alignment failed: {error}") from error
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
        return [Word(index * span / len(tokens), (index + 1) * span / len(tokens), token, 0, index) for index, token in enumerate(tokens)] if tokens else []

    def align_long_text(self, audio, text, language):
        return self.align(audio, text, language)

    def transcribe(self, audio, language):
        return "", []
