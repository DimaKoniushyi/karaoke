import numpy as np

from AI.engines.pitch import _frames_from_frequencies, _prepare_model_signal


def test_frames_from_frequencies_masks_out_of_range_pitches_to_unvoiced_zero():
    frames = _frames_from_frequencies([50.0, 220.0, 1500.0, 440.0], step=0.01, fmin=55.0, fmax=1400.0)
    assert [(f.frequency, f.confidence, f.voiced) for f in frames] == [
        (0.0, 0.0, False),
        (220.0, 1.0, True),
        (0.0, 0.0, False),
        (440.0, 1.0, True),
    ]


def test_frames_from_frequencies_includes_the_boundary_values():
    frames = _frames_from_frequencies([55.0, 1400.0], step=0.01, fmin=55.0, fmax=1400.0)
    assert [(f.frequency, f.voiced) for f in frames] == [(55.0, True), (1400.0, True)]


def test_frames_from_frequencies_spaces_frame_times_by_step():
    frames = _frames_from_frequencies([100.0, 100.0, 100.0], step=0.02, fmin=55.0, fmax=1400.0)
    assert [f.time for f in frames] == [0.0, 0.02, 0.04]


def test_frames_from_frequencies_handles_an_empty_input():
    assert _frames_from_frequencies([], step=0.01, fmin=55.0, fmax=1400.0) == []


def test_fcpe_signal_is_peak_normalized_without_clipping_its_waveform():
    source = np.array([0.0, 0.5, -1.05, np.nan], dtype=np.float32)

    prepared = _prepare_model_signal(source)

    assert np.isfinite(prepared).all()
    assert np.max(np.abs(prepared)) == 1.0
    assert np.isclose(prepared[1] / prepared[2], np.float32(0.5 / -1.05))
