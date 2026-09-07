
from __future__ import annotations

import bisect
import json
import math
import statistics
from dataclasses import dataclass
from typing import Any

import models
from AI.notes import hz_to_midi
from app.services import ai_bridge, song_service
from app.utils.json_files import read_json

_HIT_TOLERANCE_SEMITONES = 0.5
_HOLD_TOLERANCE_SEMITONES = 1.0
_RHYTHM_WINDOW_SECONDS = 0.35
_NOTE_OFFSETS = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}


def _overall_score(pitch: float, rhythm: float, hold: float, coverage: float) -> float:
    return round(pitch * 0.50 + rhythm * 0.25 + hold * 0.15 + coverage * 0.10, 1)


def _playback_segments(recording: models.Recording) -> list[dict[str, float]]:
    raw = getattr(recording, "playback_segments_json", None)
    if not raw: return []
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return []
    segments = []
    for value in values if isinstance(values, list) else []:
        try:
            start = float(value["start_recording_sec"])
            end = float(value["end_recording_sec"])
            playback = float(value["start_playback_sec"])
        except (KeyError, TypeError, ValueError):
            continue
        if start >= 0 and end > start:
            segments.append({
                "start_recording_sec": start,
                "end_recording_sec": end,
                "start_playback_sec": playback,
            })
    return sorted(segments, key=lambda segment: segment["start_recording_sec"])


def _song_time_for_recording_frame(
    timestamp: float,
    segments: list[dict[str, float]],
    fallback_offset: float,
) -> float | None:
    if not segments: return timestamp + fallback_offset
    for segment in segments:
        start, end = segment["start_recording_sec"], segment["end_recording_sec"]
        if start <= timestamp <= end:
            return segment["start_playback_sec"] + timestamp - start
    return None


@dataclass(frozen=True, slots=True)
class ReferenceIndex:
    starts: tuple[float, ...]
    notes: tuple[tuple[float, int | None], ...]

    @classmethod
    def build(cls, reference_notes: list[dict[str, Any]]) -> ReferenceIndex:
        # dict.get(key, default) only substitutes default when the key is
        # MISSING, not when its value is an explicit null -- a note with
        # "start": null (or a non-numeric start) used to crash the whole
        # comparison via float(None), unlike _normalized_reference_notes'
        # isinstance guard a few lines below for this exact same input (see
        # analyze_recording). Notes with no usable start are dropped instead;
        # an invalid/missing end still falls back to using start as before.
        valid = [note for note in reference_notes if isinstance(note.get("start"), (int, float))]
        normalized = sorted(valid, key=lambda note: float(note["start"]))
        return cls(
            starts=tuple(float(note["start"]) for note in normalized),
            notes=tuple(
                (
                    float(note["end"]) if isinstance(note.get("end"), (int, float)) else float(note["start"]),
                    _to_midi(note.get("note")),
                )
                for note in normalized
            ),
        )

    def note_at(self, timestamp: float) -> int | None:
        index = bisect.bisect_right(self.starts, timestamp) - 1
        if index < 0: return None
        end, midi = self.notes[index]
        return midi if timestamp < end else None


def _to_midi(value: object) -> int | None:
    if isinstance(value, (int, float)): return int(round(value))
    if not isinstance(value, str): return None

    value = value.strip()
    if len(value) < 2 or value[0].upper() not in _NOTE_OFFSETS: return None

    letter, accidental = value[0].upper(), value[1] if len(value) > 1 and value[1] in {'#', 'b'} else ''
    octave_text = value[2:] if accidental else value[1:]
    try:
        octave = int(octave_text)
    except ValueError:
        return None

    accidental_offset = 1 if accidental == "#" else -1 if accidental == "b" else 0
    return (octave + 1) * 12 + _NOTE_OFFSETS[letter] + accidental_offset


def _precise_midi(frame: dict[str, Any], fallback: int) -> float:
    freq = frame.get("freq") if isinstance(frame.get("freq"), (int, float)) else frame.get("frequency")
    if not isinstance(freq, (int, float)) or freq <= 0: return float(fallback)
    midi = hz_to_midi(freq)
    return midi if math.isfinite(midi) else float(fallback)


def _normalized_reference_notes(reference_notes: list[dict[str, Any]]) -> list[dict[str, float | int]]:
    normalized = []
    for note in reference_notes:
        start, end, midi = note.get("start"), note.get("end"), _to_midi(note.get("note"))
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)) or midi is None:
            continue
        if float(end) <= float(start): continue
        normalized.append({"start": float(start), "end": float(end), "midi": midi})
    return sorted(normalized, key=lambda note: note["start"])


def _take_cutoff_song_time(
    playback_segments: list[dict[str, float]],
    pitch_frames: list[dict[str, Any]],
    fallback_offset: float,
) -> float | None:
    """How far into the song this take actually reached, win or lose the mic.

    Rhythm and coverage must only be scored against notes the singer could
    have attempted; grading notes after an early stop as missed would punish
    a perfect partial take. Segments (exact play/pause/seek tracking) are the
    precise source when present; legacy takes without them fall back to the
    furthest raw frame timestamp the vocal analyzer produced.
    """
    if playback_segments:
        return max(
            segment["start_playback_sec"] + (segment["end_recording_sec"] - segment["start_recording_sec"])
            for segment in playback_segments
        )
    timestamps = [
        float(frame["time"]) for frame in pitch_frames if isinstance(frame.get("time"), (int, float))
    ]
    return max(timestamps) + fallback_offset if timestamps else None


def _singing_metrics(
    reference_notes: list[dict[str, float | int]],
    voiced_frames: list[tuple[float, int]],
    deviations: list[float],
    pitch_accuracy: float | None,
    cutoff_song_time: float | None,
) -> dict[str, float | None]:
    if pitch_accuracy is None or not voiced_frames or not reference_notes:
        return {
            "rhythm_accuracy_percent": None,
            "note_hold_percent": None,
            "note_coverage_percent": None,
            "overall_score_percent": None,
        }

    voiced_frames.sort(key=lambda frame: frame[0])
    times = [frame[0] for frame in voiced_frames]
    attempted_notes = (
        [note for note in reference_notes if float(note["start"]) < cutoff_song_time]
        if cutoff_song_time is not None
        else reference_notes
    )
    covered, rhythm_scores = 0, []
    for note in attempted_notes:
        start, end, target = float(note["start"]), float(note["end"]), int(note["midi"])
        note_left = bisect.bisect_left(times, start)
        note_right = bisect.bisect_left(times, end, lo=note_left)
        if note_right > note_left: covered += 1

        search_left = bisect.bisect_left(times, start - _RHYTHM_WINDOW_SECONDS)
        search_right = bisect.bisect_right(times, start + _RHYTHM_WINDOW_SECONDS, lo=search_left)
        matching = [
            abs(timestamp - start)
            for timestamp, midi in voiced_frames[search_left:search_right]
            if abs(midi - target) <= _HOLD_TOLERANCE_SEMITONES
        ]
        timing_error = min(matching) if matching else _RHYTHM_WINDOW_SECONDS
        rhythm_scores.append(max(0.0, 1.0 - timing_error / _RHYTHM_WINDOW_SECONDS) * 100)

    rhythm = round(statistics.fmean(rhythm_scores), 1) if rhythm_scores else None
    hold = round(
        sum(value <= _HOLD_TOLERANCE_SEMITONES for value in deviations) / len(deviations) * 100,
        1,
    )
    coverage = round(covered / len(attempted_notes) * 100, 1) if attempted_notes else None
    overall = _overall_score(pitch_accuracy, rhythm or 0.0, hold, coverage or 0.0)
    return {
        "rhythm_accuracy_percent": rhythm,
        "note_hold_percent": hold,
        "note_coverage_percent": coverage,
        "overall_score_percent": overall,
    }


def _read_optional_structure(path) -> Any:
    # A crash mid-write (or any other interference) can leave structure.json
    # partially written/corrupt on disk. Its absence is already tolerated
    # below (isinstance(structure, list)), so malformed content should
    # degrade the same way instead of raising json.JSONDecodeError out of
    # analyze_recording and failing the whole recording analysis over an
    # optional, best-effort section breakdown.
    try:
        return read_json(path)
    except (OSError, ValueError, TypeError):
        return None


def analyze_recording(recording: models.Recording, song: models.Song) -> dict[str, Any]:
    if not song.output_dir: raise ValueError("Песня ещё не обработана — нет эталонной мелодии для сравнения")

    output_dir = song_service.resolve_output_dir(song)
    reference_notes, structure = ai_bridge.get_reference_notes(output_dir), _read_optional_structure(output_dir / 'structure.json')
    if not reference_notes: raise ValueError("Не найдены вокальные ноты — мелодия ещё не построена")

    normalized_reference = _normalized_reference_notes(reference_notes)
    reference_index, pitch_frames = ReferenceIndex.build(reference_notes), ai_bridge.analyze_vocal(recording.path)
    deviations: list[float] = []
    frames: list[dict[str, float]] = []
    voiced_frames: list[tuple[float, int]] = []
    hits = 0

    playback_offset_sec = float(getattr(recording, "playback_offset_sec", 0) or 0)
    playback_segments = _playback_segments(recording)
    for frame in pitch_frames:
        timestamp = frame.get("time")
        user_midi = _to_midi(frame.get("midi") or frame.get("note"))
        if not isinstance(timestamp, (int, float)) or user_midi is None: continue
        # Map take time through the exact play/pause/seek segments captured by
        # the recorder. Legacy takes without segments retain offset behaviour.
        song_time = _song_time_for_recording_frame(
            float(timestamp), playback_segments, playback_offset_sec
        )
        if song_time is None: continue
        voiced_frames.append((song_time, user_midi))
        reference_midi = reference_index.note_at(song_time)
        if reference_midi is None: continue
        # Compare against the un-rounded pitch, not the display-rounded
        # "midi"/"note" fields: those are already quantized to the nearest
        # semitone, so a deviation between two rounded integers can only ever
        # land on a whole number and _HIT_TOLERANCE_SEMITONES (0.5) becomes an
        # exact-match-only test with no tolerance at all. The reference note
        # itself stays quantized -- it names a discrete target note -- only
        # the singer's actual pitch needs sub-semitone precision.
        precise_midi = _precise_midi(frame, user_midi)
        deviation = abs(precise_midi - reference_midi)
        deviations.append(deviation)
        frames.append({"time": song_time, "deviation_semitones": round(deviation, 3)})
        hits += deviation <= _HIT_TOLERANCE_SEMITONES

    accuracy = round(hits / len(deviations) * 100, 1) if deviations else None
    mean_deviation = round(statistics.fmean(deviations), 3) if deviations else None
    sections = _sections_breakdown(structure, frames) if isinstance(structure, list) else None
    cutoff_song_time = _take_cutoff_song_time(playback_segments, pitch_frames, playback_offset_sec)
    return {
        "pitch_accuracy_percent": accuracy,
        "mean_deviation_semitones": mean_deviation,
        **_singing_metrics(normalized_reference, voiced_frames, deviations, accuracy, cutoff_song_time),
        "sections": sections,
    }


def _sections_breakdown(structure: list[dict], frames: list[dict[str, float]]) -> list[dict]:
    result: list[dict] = []
    times = [frame["time"] for frame in frames]
    for section in structure:
        start, end = section.get("start"), section.get("end")
        if not isinstance(start, (int, float)) or not isinstance(end, (int, float)): continue
        left = bisect.bisect_left(times, float(start))
        right = bisect.bisect_left(times, float(end), lo=left)
        deviations = [frame["deviation_semitones"] for frame in frames[left:right]]
        result.append(
            {
                "label": section.get("label", section.get("name")),
                "start": start,
                "end": end,
                "accuracy_percent": (
                    round(
                        sum(value <= _HIT_TOLERANCE_SEMITONES for value in deviations)
                        / len(deviations)
                        * 100,
                        1,
                    )
                    if deviations
                    else None
                ),
                "mean_deviation_semitones": (
                    round(statistics.fmean(deviations), 3) if deviations else None
                ),
            }
        )
    return result
