from __future__ import annotations

from collections.abc import Callable

from ..models import Word

_MIN_TRUSTED_CONFIDENCE = 0.3
_MIN_FALLBACK_GAIN = 0.1


def mean_word_confidence(words: list[Word]) -> float:
    return sum(word.confidence for word in words) / max(1, len(words))


def select_stronger_ctc_alignment(
    primary: list[Word],
    fallback: Callable[[], list[Word] | None],
    *,
    label: str,
) -> list[Word]:
    """Replace an acoustically weak bounded alignment only when clearly better."""
    primary_confidence = mean_word_confidence(primary)
    if primary_confidence >= _MIN_TRUSTED_CONFIDENCE:
        return primary
    candidate = fallback()
    if not candidate:
        return primary
    candidate_confidence = mean_word_confidence(candidate)
    if candidate_confidence < primary_confidence + _MIN_FALLBACK_GAIN:
        return primary
    print(
        f"[AI] low-confidence {label} CTC replaced by full CTC "
        f"({primary_confidence:.3f} -> {candidate_confidence:.3f})",
        flush=True,
    )
    return candidate
