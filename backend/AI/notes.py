from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
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
        if owner is None or word.end - owner[1] < disconnected_tail_seconds:
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


def _segments(
    frames: list[PitchFrame],
    gap: float,
    split: float,
    *,
    change_confirmation_frames: int = 3,
):
    """Split voiced pitch into notes without mistaking vibrato for boundaries.

    A real note transition remains away from the established pitch for several
    frames.  Vibrato, pitch-estimator jitter and single-frame glitches normally
    return immediately.  Keep those provisional frames in the current note,
    while preserving the first frame of a confirmed transition as its onset.
    """
    current: list[PitchFrame] = []
    pending: list[PitchFrame] = []
    for frame in frames:
        if not frame.voiced:
            if current:
                current.extend(pending)
                yield current
                current = []
                pending = []
            continue

        previous = pending[-1] if pending else (current[-1] if current else None)
        if previous is not None and frame.time - previous.time > gap:
            current.extend(pending)
            yield current
            current = []
            pending = []

        if not current:
            current.append(frame)
            continue

        # Compare with the last accepted frame so a gradual slide remains a
        # single gesture.  Once a sudden departure appears, keep comparing
        # with that stable anchor until it either returns (vibrato/jitter) or
        # persists long enough to become a real new note.
        anchor = hz_to_midi(current[-1].frequency)
        if abs(hz_to_midi(frame.frequency) - anchor) >= split:
            pending.append(frame)
            if len(pending) >= change_confirmation_frames:
                yield current
                current = pending
                pending = []
            continue

        if pending:
            current.extend(pending)
            pending = []
        current.append(frame)
    if current:
        current.extend(pending)
        yield current


def _owner(
    words: list[Word],
    onset: float,
    tolerance: float,
    starts: list[float] | None = None,
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


def _split_segments_at_word_starts(
    segments: list[list[PitchFrame]],
    word_starts: list[float],
    *,
    minimum_dropout: float = 0.03,
) -> list[list[PitchFrame]]:
    """Keep a brief inter-word dropout from swallowing the next lyric word.

    The acoustic segmenter intentionally bridges brief unvoiced gaps inside a
    sustained syllable.  At a lyric boundary, however, the same observed gap
    separates two words.  Split only when there is an actual dropout so a
    continuously held pitch is not fragmented at every lexical boundary.
    """
    if not segments or len(word_starts) < 2:
        return segments
    boundaries = sorted(set(word_starts[1:]))
    result: list[list[PitchFrame]] = []
    for segment in segments:
        times = [frame.time for frame in segment]
        lower = bisect_right(boundaries, times[0])
        upper = bisect_right(boundaries, times[-1])
        cursor = 0
        for boundary in boundaries[lower:upper]:
            cut = bisect_left(times, boundary, lo=cursor)
            has_dropout = (
                0 < cut < len(segment)
                and segment[cut].time - segment[cut - 1].time >= minimum_dropout
            )
            if not has_dropout:
                continue
            if cut > cursor:
                result.append(segment[cursor:cut])
            cursor = cut
        if cursor < len(segment):
            result.append(segment[cursor:])
    return result


def build_vocal_notes(
    pitch: list[PitchFrame],
    _syllables=(),
    *,
    min_note=0.07,
    split_semitones=0.78,
    max_gap=0.08,
    min_confidence=0.38,
    words: list[Word] | None = None,
    word_boundary_tolerance=0.12,
    **_context,
) -> list[VocalNote]:
    frames = [frame for frame in pitch if frame.voiced and frame.confidence >= min_confidence]
    lyric_words = words or []
    word_starts = [word.start for word in lyric_words]
    notes: list[VocalNote] = []
    last_owned_end: dict[int, float] = {}
    segments = _split_segments_at_word_starts(
        list(_segments(frames, max_gap, split_semitones)),
        word_starts,
    )
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
        owner = _owner(lyric_words, start, word_boundary_tolerance, word_starts)
        if owner is None and lyric_words:
            # Long melismas and ad-libs often begin just outside the fixed word
            # boundary. Expand only for this segment, with a bounded window, so
            # those notes stay attached without claiming distant instrumental
            # pitch as part of a lyric.
            adaptive_tolerance = max(float(word_boundary_tolerance), min(0.5, 2.0 * (end - start)))
            owner = _owner(lyric_words, start, adaptive_tolerance, word_starts)
        if owner is None:
            continue
        previous_end = last_owned_end.get(owner.index)
        ownership_gap = max(0.2, float(word_boundary_tolerance))
        if previous_end is not None and start - previous_end > ownership_gap:
            # A fitted word interval is deliberately wider than its detected
            # pitch so karaoke rendering can retain a small phrase tail. Do
            # not let that wider interval claim a later disconnected cluster;
            # otherwise every reprocess expands the final word once more.
            continue
        notes.append(VocalNote(start, end, midi, word_index=owner.index))
        last_owned_end[owner.index] = end
    return notes


def retune_notes_to_pitch(
    notes: list[VocalNote],
    pitch_frames: list[PitchFrame] | tuple[PitchFrame, ...],
    *,
    min_confidence: float = 0.38,
) -> list[VocalNote]:
    """Recalculate pitch after final word-boundary clipping without moving notes."""
    frames = list(pitch_frames)
    if not notes or not frames:
        return list(notes)
    times = [frame.time for frame in frames]
    result: list[VocalNote] = []
    for note in notes:
        lower = bisect_left(times, note.start)
        upper = bisect_right(times, note.end)
        local = [
            hz_to_midi(frame.frequency)
            for frame in frames[lower:upper]
            if frame.voiced and frame.confidence >= min_confidence
        ]
        midi = min(127, max(0, round(median(local)))) if len(local) >= 3 else note.midi_note
        cents_shift = (note.midi_note - midi) * 100
        result.append(
            VocalNote(
                note.start,
                note.end,
                midi,
                velocity=note.velocity,
                word_index=note.word_index,
                syllable_index=note.syllable_index,
                cents=tuple((relative, cents + cents_shift) for relative, cents in note.cents),
                syllable_indices=note.syllable_indices,
            )
        )
    return result


def fit_notes_to_sung_words(
    words: list[Word],
    notes: list[VocalNote],
    *,
    pitch_frames: list[PitchFrame] | tuple[PitchFrame, ...] = (),
    duration: float | None = None,
    word_end_limits: dict[int, float] | None = None,
    contiguous_gap: float = 0.2,
    phrase_tail: float = 0.25,
    max_note_stretch: float = 1.0,
) -> tuple[list[Word], list[VocalNote]]:
    """Expand narrow CTC emissions into karaoke-style sung word intervals.

    CTC timestamps describe the token's strongest acoustic emission, while a
    karaoke note describes the complete sung slot.  Preserve the detected
    pitch changes, but scale their timing from the word onset to either the
    following connected word or a bounded phrase tail.
    """
    if not words or (not notes and not pitch_frames):
        return words, notes
    by_word: dict[int, list[VocalNote]] = {}
    for note in notes:
        if note.word_index is not None:
            by_word.setdefault(note.word_index, []).append(note)
    fitted_words: list[Word] = []
    fitted_notes: list[VocalNote] = []
    for position, word in enumerate(words):
        owned = sorted(by_word.get(word.index, ()), key=lambda note: note.start)
        following_start = words[position + 1].start if position + 1 < len(words) else duration
        if not owned:
            end = word.end
            if following_start is not None and following_start > word.start:
                end = min(end, following_start)
            local_pitch = [
                frame
                for frame in pitch_frames
                if frame.voiced and word.start <= frame.time <= word.end
            ]
            nearest = min(
                notes,
                key=lambda note: abs(note.start - word.start),
                default=None,
            )
            local_midi = (
                round(median(hz_to_midi(frame.frequency) for frame in local_pitch))
                if len(local_pitch) >= 3
                else None
            )
            nearest_is_usable = nearest is not None and abs(nearest.start - word.start) <= 0.75
            if local_midi is not None or nearest_is_usable:
                if following_start is not None and 0 < following_start - word.start <= 1.5:
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
                        local_midi if local_midi is not None else nearest.midi_note,
                        velocity=nearest.velocity if nearest is not None else 96,
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
            if position + 1 < len(words)
            else []
        )
        next_voice = next_owned[0].start if next_owned else following_start
        acoustic_gap = float(next_voice) - last if next_voice is not None else float("inf")
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
        note_origin = max(word.start, first)
        # Word intervals may legitimately extend to the next lyric onset, but
        # the measured vocal note must not be warped to fill that complete
        # lexical slot.  Large stretching erased rests and produced note
        # lengths far beyond both the vocal stem and authored karaoke scores.
        scale = min(
            max(0.001, target_end - note_origin) / source_span,
            max(1.0, float(max_note_stretch)),
        )
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
            start = note_origin + (note.start - first) * scale
            end = note_origin + (note.end - first) * scale
            fitted_notes.append(
                VocalNote(
                    start,
                    min(target_end, end),
                    note.midi_note,
                    velocity=note.velocity,
                    word_index=word.index,
                    syllable_index=note.syllable_index,
                    cents=tuple((relative * scale, cents) for relative, cents in note.cents),
                    syllable_indices=note.syllable_indices,
                )
            )
    return fitted_words, fitted_notes


def fit_notes_with_refined_ownership(
    words: list[Word],
    notes: list[VocalNote],
    *,
    pitch_frames: list[PitchFrame] | tuple[PitchFrame, ...],
    note_options: dict | None = None,
    **fit_options,
) -> tuple[list[Word], list[VocalNote]]:
    """Refit pitch after narrow CTC intervals have expanded to sung slots."""
    fitted_words, fitted_notes = fit_notes_to_sung_words(
        words,
        notes,
        pitch_frames=pitch_frames,
        **fit_options,
    )
    if not pitch_frames:
        return fitted_words, fitted_notes
    refined = build_vocal_notes(
        list(pitch_frames),
        words=fitted_words,
        **(note_options or {}),
    )
    if not refined:
        return fitted_words, retune_notes_to_pitch(fitted_notes, pitch_frames)
    final_words, final_notes = fit_notes_to_sung_words(
        fitted_words, refined, pitch_frames=pitch_frames, **fit_options
    )
    return final_words, retune_notes_to_pitch(final_notes, pitch_frames)


def build_game_notes(*args, **kwargs):
    return build_vocal_notes(*args, **kwargs)


def get_note_diagnostics() -> dict:
    return {}
