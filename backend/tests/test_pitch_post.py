from AI.models import PitchFrame
from AI.pitch_post import stabilize_pitch


def frame(time, frequency, voiced=True, confidence=1.0, energy=1.0):
    return PitchFrame(time, frequency, confidence, voiced, energy)


def test_stabilize_pitch_smooths_a_single_frame_spike_using_its_voiced_neighbors():
    frames = [
        frame(0.0, 100.0),
        frame(0.1, 500.0),
        frame(0.2, 100.0),
        frame(0.3, 100.0),
        frame(0.4, 100.0),
    ]
    result = stabilize_pitch(frames)
    assert [item.frequency for item in result] == [100.0, 100.0, 100.0, 100.0, 100.0]


def test_stabilize_pitch_removes_a_single_frame_two_semitone_glitch_by_default():
    base = 440.0
    two_semitones_up = base * (2 ** (2 / 12))
    frames = [frame(0.0, base), frame(0.01, two_semitones_up), frame(0.02, base)]

    result = stabilize_pitch(frames)

    assert result[1].frequency == base


def test_stabilize_pitch_leaves_the_first_and_last_frame_untouched():
    frames = [frame(0.0, 999.0), frame(0.1, 500.0), frame(0.2, 999.0)]
    result = stabilize_pitch(frames)
    assert result[0] is frames[0]
    assert result[2] is frames[2]


def test_stabilize_pitch_never_updates_an_unvoiced_frame_even_with_voiced_neighbors():
    # PitchFrame itself normalizes an unvoiced frame's frequency to 0 --
    # what matters here is that stabilize_pitch doesn't even try to touch it.
    frames = [frame(0.0, 100.0), frame(0.1, 500.0, voiced=False), frame(0.2, 100.0)]
    result = stabilize_pitch(frames)
    assert result[1] is frames[1]


def test_stabilize_pitch_skips_a_voiced_frame_with_fewer_than_two_voiced_neighbors():
    frames = [
        frame(0.0, 100.0, voiced=False),
        frame(0.1, 500.0),
        frame(0.2, 100.0, voiced=False),
    ]
    result = stabilize_pitch(frames)
    assert result[1] is frames[1]


def test_stabilize_pitch_preserves_confidence_energy_and_time_on_a_smoothed_frame():
    frames = [
        frame(0.0, 100.0, confidence=0.4, energy=0.7),
        frame(0.1, 500.0, confidence=0.9, energy=0.3),
        frame(0.2, 100.0, confidence=0.2, energy=0.6),
    ]
    result = stabilize_pitch(frames)
    smoothed = result[1]
    assert smoothed.frequency == 100.0
    assert (smoothed.time, smoothed.confidence, smoothed.energy) == (0.1, 0.9, 0.3)
    assert smoothed.voiced is True


def test_stabilize_pitch_returns_frames_unchanged_when_fewer_than_three():
    frames = [frame(0.0, 100.0), frame(0.1, 200.0)]
    assert stabilize_pitch(frames) == frames


def test_stabilize_pitch_preserves_a_legitimate_jump_below_the_threshold():
    frames = [frame(0.0, 100.0), frame(0.1, 150.0), frame(0.2, 100.0)]
    result = stabilize_pitch(frames, max_octave_jump=10.5)
    assert result[1] is frames[1]


def test_stabilize_pitch_threshold_controls_octave_error_filtering():
    frames = [frame(0.0, 100.0), frame(0.1, 200.0), frame(0.2, 100.0)]
    assert stabilize_pitch(frames, max_octave_jump=11.5)[1].frequency == 100.0
    assert stabilize_pitch(frames, max_octave_jump=12.5)[1] is frames[1]
