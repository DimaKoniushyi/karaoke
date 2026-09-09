import AI.notes as notes_module
from AI.models import PitchFrame, VocalNote, Word
from AI.notes import (
    build_vocal_notes,
    constrain_line_final_words_to_voice,
    fit_notes_to_sung_words,
    retune_notes_to_pitch,
)


def test_long_melisma_just_outside_word_uses_bounded_adaptive_tolerance():
    pitch = [PitchFrame(time, 440.0, 1.0, True, 1.0) for time in (0.45, 0.50, 0.55, 0.60, 0.65)]
    words = [Word(0.9, 1.2, "la", index=0)]

    notes = build_vocal_notes(pitch, words=words, word_boundary_tolerance=0.12, max_gap=0.06)

    assert len(notes) == 1
    assert notes[0].word_index == 0


def test_distant_pitch_is_not_claimed_by_a_word():
    pitch = [PitchFrame(time, 440.0, 1.0, True, 1.0) for time in (0.0, 0.05, 0.10, 0.15)]
    words = [Word(1.0, 1.2, "la", index=0)]

    assert build_vocal_notes(pitch, words=words) == []


def test_narrow_ctc_word_expands_without_warping_its_physical_note():
    words = [
        Word(1.0, 1.05, "первая", index=0),
        Word(1.2, 1.25, "вторая", index=1),
    ]
    notes = [
        VocalNote(1.0, 1.05, 60, word_index=0),
        VocalNote(1.2, 1.25, 62, word_index=1),
    ]

    fitted_words, fitted_notes = fit_notes_to_sung_words(words, notes)

    assert fitted_words[0].end == 1.2
    assert fitted_notes[0].start == 1.0
    assert fitted_notes[0].end == 1.05


def test_physical_note_duration_is_not_stretched_to_fill_a_lexical_slot():
    words = [
        Word(1.0, 1.1, "первая", index=0),
        Word(1.4, 1.5, "вторая", index=1),
    ]
    notes = [
        VocalNote(1.05, 1.25, 60, word_index=0),
        VocalNote(1.43, 1.55, 62, word_index=1),
    ]

    _fitted_words, fitted_notes = fit_notes_to_sung_words(words, notes)

    first = next(note for note in fitted_notes if note.word_index == 0)
    assert first.end - first.start <= 0.221


def test_physical_note_duration_is_preserved_when_word_slot_is_longer():
    words = [
        Word(1.0, 1.1, "первая", index=0),
        Word(1.4, 1.5, "вторая", index=1),
    ]
    notes = [
        VocalNote(1.05, 1.25, 60, word_index=0),
        VocalNote(1.43, 1.55, 62, word_index=1),
    ]

    _fitted_words, fitted_notes = fit_notes_to_sung_words(words, notes)

    first = next(note for note in fitted_notes if note.word_index == 0)
    assert abs((first.end - first.start) - 0.2) < 1e-9


def test_physical_note_onset_is_preserved_when_voice_starts_after_word_timestamp():
    words = [
        Word(1.0, 1.1, "слово", index=0),
        Word(1.6, 1.7, "дальше", index=1),
    ]
    notes = [
        VocalNote(1.18, 1.42, 60, word_index=0),
        VocalNote(1.62, 1.72, 62, word_index=1),
    ]

    _fitted_words, fitted_notes = fit_notes_to_sung_words(words, notes)

    first = next(note for note in fitted_notes if note.word_index == 0)
    assert first.start == 1.18
    assert first.end == 1.42


def test_clipped_note_is_retuned_from_pitch_inside_its_final_interval():
    note = VocalNote(
        1.2,
        1.5,
        60,
        velocity=87,
        word_index=3,
        syllable_index=1,
    )
    pitch = [PitchFrame(time, 329.627557, 1.0, True, 1.0) for time in (1.21, 1.28, 1.36, 1.44)]

    [retuned] = retune_notes_to_pitch([note], pitch)

    assert retuned.midi_note == 64
    assert (retuned.start, retuned.end) == (note.start, note.end)
    assert retuned.velocity == 87
    assert retuned.word_index == 3
    assert retuned.syllable_index == 1


def test_phrase_final_note_gets_a_bounded_tail_instead_of_crossing_the_pause():
    words = [
        Word(1.0, 2.8, "конец", index=0),
        Word(3.0, 3.1, "дальше", index=1),
    ]
    notes = [
        VocalNote(1.0, 1.2, 60, word_index=0),
        VocalNote(3.0, 3.1, 62, word_index=1),
    ]

    fitted_words, fitted_notes = fit_notes_to_sung_words(words, notes)

    assert fitted_words[0].end == 1.45
    assert fitted_notes[0].end == 1.2


def test_note_near_a_new_word_is_not_claimed_by_an_overlapping_previous_word():
    pitch = [PitchFrame(time, 440.0, 1.0, True, 1.0) for time in (2.0, 2.05, 2.1, 2.15)]
    words = [
        Word(1.0, 3.0, "предыдущее", index=0),
        Word(2.02, 2.3, "новое", index=1),
    ]

    [note] = build_vocal_notes(pitch, words=words, max_gap=0.06)

    assert note.word_index == 1


def test_vibrato_around_one_pitch_remains_one_sustained_note():
    pitch = [
        PitchFrame(
            index * 0.01,
            440.0 * (2 ** ((0.42 if index % 2 else -0.42) / 12)),
            1.0,
            True,
            1.0,
        )
        for index in range(30)
    ]
    words = [Word(0.0, 0.4, "долго", index=0)]

    notes = build_vocal_notes(pitch, words=words)

    assert len(notes) == 1
    assert notes[0].start == 0.0
    assert notes[0].end == 0.3


def test_brief_pitch_dropout_inside_one_sung_word_does_not_split_the_note():
    pitch = [
        PitchFrame(time, 440.0, 1.0, True, 1.0)
        for time in (0.0, 0.02, 0.04, 0.06, 0.13, 0.15, 0.17, 0.19)
    ]
    words = [Word(0.0, 0.3, "долго", index=0)]

    notes = build_vocal_notes(pitch, words=words)

    assert len(notes) == 1
    assert notes[0].start == 0.0
    assert notes[0].end >= 0.2


def test_brief_pitch_dropout_between_words_still_splits_the_note():
    pitch = [
        PitchFrame(time, 440.0, 1.0, True, 1.0)
        for time in (0.0, 0.02, 0.04, 0.06, 0.13, 0.15, 0.17, 0.19)
    ]
    words = [
        Word(0.0, 0.08, "два", index=0),
        Word(0.13, 0.25, "слова", index=1),
    ]

    notes = build_vocal_notes(pitch, words=words)

    assert [(note.word_index, note.start) for note in notes] == [(0, 0.0), (1, 0.13)]


def test_sustained_pitch_change_still_starts_a_new_note():
    pitch = [PitchFrame(index * 0.01, 440.0, 1.0, True, 1.0) for index in range(15)] + [
        PitchFrame(index * 0.01, 493.883, 1.0, True, 1.0) for index in range(15, 30)
    ]
    words = [Word(0.0, 0.4, "две", index=0)]

    notes = build_vocal_notes(pitch, words=words)

    assert len(notes) == 2
    assert [note.midi_note for note in notes] == [69, 71]
    assert notes[0].end == 0.15
    assert notes[1].start == 0.15


def test_short_word_between_sung_words_recovers_a_note_from_nearby_pitch():
    words = [
        Word(1.0, 1.1, "до", index=0),
        Word(1.2, 1.25, "я", index=1),
        Word(1.4, 1.5, "после", index=2),
    ]
    notes = [
        VocalNote(1.0, 1.1, 60, word_index=0),
        VocalNote(1.4, 1.5, 62, word_index=2),
    ]

    fitted_words, fitted_notes = fit_notes_to_sung_words(words, notes)

    recovered = [note for note in fitted_notes if note.word_index == 1]
    assert fitted_words[1].end == 1.4
    assert len(recovered) == 1
    assert recovered[0].start == 1.2
    assert recovered[0].end == 1.4


def test_missing_word_uses_its_own_pitch_frames_before_a_neighbor_note():
    words = [
        Word(1.0, 1.1, "до", index=0),
        Word(1.2, 1.3, "я", index=1),
    ]
    notes = [VocalNote(1.0, 1.1, 60, word_index=0)]
    pitch = [PitchFrame(time, 523.251, 0.9, True, 1.0) for time in (1.21, 1.23, 1.25, 1.27)]

    _words, fitted_notes = fit_notes_to_sung_words(words, notes, pitch_frames=pitch)

    recovered = [note for note in fitted_notes if note.word_index == 1]
    assert len(recovered) == 1
    assert recovered[0].midi_note == 72


def test_local_pitch_recovers_melody_when_no_long_physical_note_survived():
    words = [Word(1.0, 1.3, "я", index=0)]
    pitch = [PitchFrame(time, 523.251, 0.9, True, 1.0) for time in (1.05, 1.10, 1.15, 1.20)]

    fitted_words, fitted_notes = fit_notes_to_sung_words(
        words,
        [],
        pitch_frames=pitch,
    )

    assert fitted_words == words
    assert len(fitted_notes) == 1
    assert fitted_notes[0].midi_note == 72
    assert fitted_notes[0].word_index == 0


def test_no_notes_and_no_pitch_leave_word_intervals_unchanged():
    words = [
        Word(1.0, 1.8, "первое", index=0),
        Word(1.5, 2.0, "второе", index=1),
    ]

    fitted_words, fitted_notes = fit_notes_to_sung_words(words, [])

    assert fitted_words == words
    assert fitted_notes == []


def test_second_ownership_pass_recovers_pitch_inside_the_expanded_word():
    words = [
        Word(1.0, 1.05, "долго", index=0),
        Word(1.5, 1.6, "дальше", index=1),
    ]
    pitch = [
        *[PitchFrame(time, 440.0, 0.9, True, 1.0) for time in (1.0, 1.02, 1.04, 1.06, 1.08)],
        *[PitchFrame(time, 493.883, 0.9, True, 1.0) for time in (1.28, 1.30, 1.32, 1.34, 1.36)],
        *[PitchFrame(time, 523.251, 0.9, True, 1.0) for time in (1.5, 1.52, 1.54, 1.56, 1.58)],
    ]
    initial = build_vocal_notes(pitch, words=words)
    assert [note.midi_note for note in initial if note.word_index == 0] == [69]

    fitted_words, fitted_notes = notes_module.fit_notes_with_refined_ownership(
        words,
        initial,
        pitch_frames=pitch,
    )

    assert fitted_words[0].end >= 1.36
    assert [note.midi_note for note in fitted_notes if note.word_index == 0] == [69, 71]


def test_repeated_refinement_cannot_grow_final_word_across_disconnected_pitch():
    words = [Word(1.0, 1.05, "финал", index=0)]
    pitch = [
        *[PitchFrame(time, 440.0, 0.9, True, 1.0) for time in (1.0, 1.02, 1.04, 1.06, 1.08)],
        *[PitchFrame(time, 493.883, 0.9, True, 1.0) for time in (1.32, 1.34, 1.36, 1.38, 1.40)],
        *[PitchFrame(time, 523.251, 0.9, True, 1.0) for time in (1.64, 1.66, 1.68, 1.70, 1.72)],
    ]
    initial = build_vocal_notes(pitch, words=words)

    first_words, first_notes = notes_module.fit_notes_with_refined_ownership(
        words,
        initial,
        pitch_frames=pitch,
    )
    second_words, second_notes = notes_module.fit_notes_with_refined_ownership(
        first_words,
        first_notes,
        pitch_frames=pitch,
    )

    assert second_words == first_words
    assert second_notes == first_notes
    assert [note.midi_note for note in first_notes] == [69]
    assert first_words[0].end == 1.35


def test_silent_word_far_from_pitch_stays_without_an_invented_note():
    words = [Word(1.0, 1.2, "тихо", index=0)]
    notes = [VocalNote(3.0, 3.2, 60, word_index=7)]

    _fitted_words, fitted_notes = fit_notes_to_sung_words(words, notes)

    assert all(note.word_index != 0 for note in fitted_notes)


def test_word_inside_the_same_line_extends_without_warping_its_note():
    words = [
        Word(1.0, 1.2, "никто", index=0),
        Word(1.35, 1.55, "не", index=1),
    ]
    notes = [
        VocalNote(1.0, 1.2, 60, word_index=0),
        VocalNote(1.35, 1.55, 62, word_index=1),
    ]

    fitted_words, fitted_notes = fit_notes_to_sung_words(
        words,
        notes,
    )

    assert fitted_words[0].end == 1.35
    assert fitted_notes[0].end == 1.2


def test_physical_note_is_not_stretched_across_a_real_silence_inside_a_line():
    words = [
        Word(1.0, 1.2, "первое", index=0),
        Word(3.0, 3.2, "второе", index=1),
    ]
    notes = [
        VocalNote(1.0, 1.2, 60, word_index=0),
        VocalNote(3.0, 3.2, 62, word_index=1),
    ]

    fitted_words, fitted_notes = fit_notes_to_sung_words(
        words,
        notes,
        phrase_tail=0.25,
    )

    assert fitted_words[0].end == 1.45
    assert fitted_notes[0].end == 1.2


def test_line_final_word_cannot_claim_a_disconnected_later_vocal_interval():
    words = [
        Word(74.4, 88.0, "метро", index=0),
        Word(87.6, 87.9, "На", index=1),
    ]

    constrained = constrain_line_final_words_to_voice(
        words,
        [(68.4, 75.56), (86.9, 89.92)],
        line_end_indices={0},
    )

    assert constrained[0].start == 74.4
    assert constrained[0].end == 75.56
    assert constrained[1] == words[1]


def test_line_final_word_keeps_a_bounded_phrase_tail():
    words = [Word(10.0, 13.0, "долго", index=0)]

    constrained = constrain_line_final_words_to_voice(
        words,
        [(9.5, 11.0)],
        line_end_indices={0},
    )

    assert constrained == words


def test_fitted_line_final_word_respects_voice_limit_without_stretching_note():
    words = [
        Word(74.4, 75.56, "метро", index=0),
        Word(87.6, 87.9, "На", index=1),
    ]
    notes = [VocalNote(74.4, 75.54, 53, word_index=0)]

    fitted_words, fitted_notes = fit_notes_to_sung_words(
        words,
        notes,
        word_end_limits={0: 75.56},
    )

    assert fitted_words[0].end == 75.56
    assert fitted_notes[0].end == 75.54
