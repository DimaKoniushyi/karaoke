from __future__ import annotations

import math
from bisect import bisect_right
from statistics import median

from .models import PitchFrame, VocalNote, Word

NOTE_DECODER_VERSION = "clean-v1"


def constrain_line_final_words_to_voice(
    words: list[Word],
    voice_intervals: list[tuple[float, float]],
    *,
    line_end_indices: set[int] | frozenset[int],
    onset_tolerance: float = 0.15,
    disconnected_tail_seconds: float = 4.0,
) -> list[Word]:
    """Stop an open-ended CTC line at its first continuous vocal interval."""
    if not words or not voice_intervals or not line_end_indices:
        return list(words)
    result = list(words)
    for position, word in enumerate(words):
        if word.index not in line_end_indices:
            continue
        owner = next(
            (
                (start, end)
                for start, end in voice_intervals
                if start - onset_tolerance <= word.start <= end + onset_tolerance
            ),
            None,
        )
        if (
            owner is None
            or word.end - owner[1] < disconnected_tail_seconds
        ):
            continue
        end = max(word.start + 0.01, owner[1])
        result[position] = Word(
            word.start,
            end,
            word.text,
            word.confidence,
            word.index,
        )
    return result


def hz_to_midi(hz: float) -> float:
    return 69 + 12 * math.log2(float(hz) / 440)


def _segments(frames: list[PitchFrame], gap: float, split: float):
    current: list[PitchFrame] = []
    for frame in frames:
        if not frame.voiced:
            if current:
                yield current
                current = []
            continue
        if current and (frame.time - current[-1].time > gap or abs(hz_to_midi(frame.frequency) - hz_to_midi(current[-1].frequency)) >= split):
            yield current
            current = []
        current.append(frame)
    if current:
        yield current


def _owner(
    words: list[Word], onset: float, tolerance: float, starts: list[float] | None = None,
) -> Word | None:
    # CTC word ends can overlap later tokens by several seconds. Ownership by
    # interval overlap therefore leaks notes from a following phrase into the
    # previous word. A note onset belongs to the latest word onset within the
    # tolerance window; the end is used only to reject genuinely distant
    # pitch.
    starts = starts if starts is not None else [word.start for word in words]
    index = bisect_right(starts, onset + tolerance) - 1
    if index < 0:
        return None
    word = words[index]
    return word if word.end + tolerance >= onset else None


def build_vocal_notes(
    pitch: list[PitchFrame],
    _syllables=(),
    *,
    min_note=0.07,
    split_semitones=0.78,
    max_gap=0.05,
    min_confidence=0.38,
    words: list[Word] | None = None,
    word_boundary_tolerance=0.12,
    **_context,
) -> list[VocalNote]:
    frames = [frame for frame in pitch if frame.voiced and frame.confidence >= min_confidence]
    lyric_words = words or []
    word_starts = [word.start for word in lyric_words]
    notes: list[VocalNote] = []
    segments = list(_segments(frames, max_gap, split_semitones))
    steps = [
        right.time - left.time
        for left, right in zip(frames, frames[1:], strict=False)
        if 0 < right.time - left.time <= max_gap
    ]
    hop = median(steps) if steps else min(max_gap, 0.01)
    for index, segment in enumerate(segments):
        start = segment[0].time
        end = segment[-1].time + hop
        if index + 1 < len(segments):
            end = min(end, segments[index + 1][0].time)
        if end - start < min_note:
            continue
        midi = round(median(hz_to_midi(frame.frequency) for frame in segment))
        owner = _owner(
            lyric_words, start, word_boundary_tolerance, word_starts
        )
        if owner is None and lyric_words:
            # Long melismas and ad-libs often begin just outside the fixed word
            # boundary. Expand only for this segment, with a bounded window, so
            # those notes stay attached without claiming distant instrumental
            # pitch as part of a lyric.
            adaptive_tolerance = max(
                float(word_boundary_tolerance), min(0.5, 2.0 * (end - start))
            )
            owner = _owner(
                lyric_words, start, adaptive_tolerance, word_starts
            )
        if owner is None:
            continue
        notes.append(VocalNote(start, end, midi, word_index=owner.index))
    return notes


def fit_notes_to_sung_words(
    words: list[Word],
    notes: list[VocalNote],
    *,
    duration: float | None = None,
    word_end_limits: dict[int, float] | None = None,
    contiguous_gap: float = 0.2,
    phrase_tail: float = 0.25,
) -> tuple[list[Word], list[VocalNote]]:
    """Expand narrow CTC emissions into karaoke-style sung word intervals.

    CTC timestamps describe the token's strongest acoustic emission, while a
    karaoke note describes the complete sung slot.  Preserve the detected
    pitch changes, but scale their timing from the word onset to either the
    following connected word or a bounded phrase tail.
    """
    if not words or not notes:
        return words, notes
    by_word: dict[int, list[VocalNote]] = {}
    for note in notes:
        if note.word_index is not None:
            by_word.setdefault(note.word_index, []).append(note)
    fitted_words: list[Word] = []
    fitted_notes: list[VocalNote] = []
    for position, word in enumerate(words):
        owned = sorted(by_word.get(word.index, ()), key=lambda note: note.start)
        following_start = (
            words[position + 1].start if position + 1 < len(words) else duration
        )
        if not owned:
            end = word.end
            if following_start is not None and following_start > word.start:
                end = min(end, following_start)
            nearest = min(
                notes,
                key=lambda note: abs(note.start - word.start),
                default=None,
            )
            if nearest is not None and abs(nearest.start - word.start) <= 0.75:
                if (
                    following_start is not None
                    and 0 < following_start - word.start <= 1.5
                ):
                    end = following_start
                else:
                    end = max(end, word.start + phrase_tail)
                    if duration is not None:
                        end = min(end, duration)
                end = max(word.start + 0.001, end)
                if word_end_limits and word.index in word_end_limits:
                    end = min(end, word_end_limits[word.index])
                fitted_notes.append(
                    VocalNote(
                        word.start,
                        end,
                        nearest.midi_note,
                        velocity=nearest.velocity,
                        word_index=word.index,
                    )
                )
            fitted_words.append(
                Word(word.start, max(word.start, end), word.text, word.confidence, word.index)
            )
            continue
        first, last = owned[0].start, owned[-1].end
        next_owned = (
            sorted(by_word.get(words[position + 1].index, ()), key=lambda note: note.start)
            if position + 1 < len(words) else []
        )
        next_voice = next_owned[0].start if next_owned else following_start
        acoustic_gap = (
            float(next_voice) - last if next_voice is not None else float("inf")
        )
        if following_start is not None and following_start > word.start:
            # A lyric line can contain an intentional breath/rest between two
            # words. Line membership alone is not acoustic evidence that the
            # first vowel continues through that silence; stretching to the
            # next token in that case manufactured long notes absent from the
            # vocal stem. Bridge only a genuinely contiguous detected phrase.
            if acoustic_gap <= contiguous_gap:
                target_end = following_start
            else:
                target_end = min(following_start, last + phrase_tail)
        else:
            target_end = last + phrase_tail
            if duration is not None:
                target_end = min(target_end, duration)
        if word_end_limits and word.index in word_end_limits:
            target_end = min(target_end, word_end_limits[word.index])
        target_end = max(word.start + 0.001, target_end)
        source_span = max(0.001, last - first)
        scale = (target_end - word.start) / source_span
        fitted_words.append(
            Word(
                word.start,
                target_end,
                word.text,
                word.confidence,
                word.index,
            )
        )
        for note in owned:
            start = word.start + (note.start - first) * scale
            end = word.start + (note.end - first) * scale
            fitted_notes.append(
                VocalNote(
                    start,
                    min(target_end, end),
                    note.midi_note,
                    velocity=note.velocity,
                    word_index=word.index,
                    syllable_index=note.syllable_index,
                    cents=tuple(
                        (relative * scale, cents)
                        for relative, cents in note.cents
                    ),
                    syllable_indices=note.syllable_indices,
                )
            )
    return fitted_words, fitted_notes


def build_game_notes(*args, **kwargs):
    return build_vocal_notes(*args, **kwargs)


def get_note_diagnostics() -> dict:
    return {}
