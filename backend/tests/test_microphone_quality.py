import math

import numpy as np
import pytest

from app.services.microphone_quality import (
    MonitorEffectsChain,
    RealtimePitchShifter,
    StudioMicrophoneProcessor,
)


def _reference_hp_tone_stage(buffers, sample_rate, channels):
    """A literal, unvectorized port of the pre-scipy per-sample recursion.

    StudioMicrophoneProcessor.process now runs this same math through
    scipy.signal.lfilter across a whole buffer instead of one sample at a
    time -- this reference implementation is kept only here, to check the
    vectorized version still produces the same highpass+tone-tilt output
    (including state carried across separate process()-sized buffers).
    """
    hp_r = math.exp(-2.0 * math.pi * 70.0 / sample_rate)
    tone_alpha = 1.0 - math.exp(-2.0 * math.pi * 2200.0 / sample_rate)
    prev_x = np.zeros(channels)
    hp_y = np.zeros(channels)
    tone_low = np.zeros(channels)
    outputs = []
    for buffer in buffers:
        y = np.empty_like(buffer)
        for index in range(len(buffer)):
            current = buffer[index]
            hp = current - prev_x + hp_r * hp_y
            prev_x = current
            hp_y = hp
            tone_low = tone_low + tone_alpha * (hp - tone_low)
            y[index] = hp * 0.94 + (hp - tone_low) * 0.16
        outputs.append(y)
    return outputs


def _vectorized_hp_tone_stage(buffers, sample_rate, channels):
    from scipy.signal import lfilter, lfiltic

    hp_r = math.exp(-2.0 * math.pi * 70.0 / sample_rate)
    tone_alpha = 1.0 - math.exp(-2.0 * math.pi * 2200.0 / sample_rate)
    hp_b, hp_a = np.array([1.0, -1.0]), np.array([1.0, -hp_r])
    tone_b, tone_a = np.array([tone_alpha]), np.array([1.0, -(1.0 - tone_alpha)])
    hp_zi = np.tile(lfiltic(hp_b, hp_a, y=[0.0], x=[0.0])[:, None], (1, channels))
    tone_zi = np.tile(lfiltic(tone_b, tone_a, y=[0.0], x=[0.0])[:, None], (1, channels))
    outputs = []
    for buffer in buffers:
        hp, hp_zi = lfilter(hp_b, hp_a, buffer, axis=0, zi=hp_zi)
        tone_low, tone_zi = lfilter(tone_b, tone_a, hp, axis=0, zi=tone_zi)
        outputs.append(hp * 0.94 + (hp - tone_low) * 0.16)
    return outputs


def test_vectorized_highpass_tone_stage_matches_the_original_per_sample_recursion():
    rng = np.random.default_rng(42)
    buffers = [rng.uniform(-1.0, 1.0, size=(size, 2)) for size in (37, 256, 1, 500)]
    expected = _reference_hp_tone_stage(buffers, 48000, 2)
    actual = _vectorized_hp_tone_stage(buffers, 48000, 2)
    for reference_output, vectorized_output in zip(expected, actual, strict=True):
        np.testing.assert_allclose(vectorized_output, reference_output, rtol=1e-9, atol=1e-12)


def test_studio_processor_suppresses_quiet_noise_and_limits_peaks():
    processor, noise = StudioMicrophoneProcessor(48000, 1), np.full((256, 1), 0.001, dtype=np.float32)
    for _ in range(12): quiet = processor.process(noise)
    assert float(np.sqrt(np.mean(quiet**2))) < 0.001

    loud = np.array([[1.4], [-1.4]] * 128, dtype=np.float32)
    processed = processor.process(loud)
    assert (processed.shape == loud.shape) and (np.max(np.abs(processed)) <= 0.985) and (np.isfinite(processed).all())


def test_studio_processor_keeps_normal_voice_audible_and_stateful():
    processor, time = StudioMicrophoneProcessor(48000, 1), np.arange(480, dtype=np.float32) / 48000
    voice = (0.18 * np.sin(2 * np.pi * 220 * time))[:, None]
    first, second = processor.process(voice), processor.process(voice)
    assert (np.sqrt(np.mean(second ** 2)) > 0.03) and (not np.allclose(first, voice)) and (np.isfinite(second).all())


def test_full_noise_suppression_has_more_range_without_muting_normal_voice():
    noise = np.full((512, 1), 0.004, dtype=np.float32)
    weak, strong = StudioMicrophoneProcessor(48000, 1), StudioMicrophoneProcessor(48000, 1)
    for _ in range(8):
        weak_result = weak.process(noise, noise_suppression=0.0)
        strong_result = strong.process(noise, noise_suppression=1.0)
    assert np.sqrt(np.mean(strong_result**2)) < np.sqrt(np.mean(weak_result**2)) * 0.35

    time = np.arange(480, dtype=np.float32) / 48000
    voice = (0.18 * np.sin(2 * np.pi * 220 * time))[:, None]
    assert np.sqrt(np.mean(strong.process(voice, noise_suppression=1.0) ** 2)) > 0.03


def test_monitor_effects_chain_vectorized_and_fallback_paths_agree():
    # process() takes a vectorized fast path when every active tap delay is
    # at least as long as the current block (true for any real audio
    # callback size), and falls back to the original sample-by-sample loop
    # otherwise. Feeding the same signal through in small chunks (fast path
    # every time) vs. one big chunk longer than the shortest tap delay
    # (forcing the fallback) must produce identical output either way, since
    # this is a strictly causal filter -- chunking must never change the
    # result.
    rng = np.random.default_rng(7)
    signal = rng.uniform(-0.5, 0.5, size=3000).astype(np.float32)

    chunked = MonitorEffectsChain(48000)
    chunked_out = np.concatenate(
        [chunked.process(signal[start:start + 100], 0.3, 0.4, 0.2) for start in range(0, 3000, 100)]
    )

    whole = MonitorEffectsChain(48000)
    whole_out = whole.process(signal, 0.3, 0.4, 0.2)

    np.testing.assert_allclose(chunked_out, whole_out, rtol=1e-6, atol=1e-7)


def test_monitor_effects_chain_is_a_passthrough_when_everything_is_off():
    chain, impulse = MonitorEffectsChain(48000), np.zeros(4000, dtype=np.float32)
    impulse[0] = 1.0
    out = chain.process(impulse, reverb=0.0, echo=0.0, delay=0.0)
    assert np.array_equal(out, impulse)


def test_monitor_effects_chain_adds_a_decaying_echo_after_the_tap_delay():
    chain, impulse = MonitorEffectsChain(48000), np.zeros(48000, dtype=np.float32)
    impulse[0] = 1.0
    out = chain.process(impulse, reverb=0.0, echo=1.0, delay=0.0)
    assert (np.isfinite(out).all()) and (np.allclose(out[1:int(0.179 * 48000)], 0.0))
    first_tap, second_tap = int(0.18 * 48000), int(0.36 * 48000)
    assert (out[first_tap] > 0.0) and (out[second_tap] > 0.0) and (abs(out[-1]) < out[first_tap] * 0.05)


def test_monitor_effects_chain_stays_bounded_with_every_effect_maxed(monkeypatch):
    chain, loud = MonitorEffectsChain(48000), np.full(96000, 0.9, dtype=np.float32)
    out = chain.process(loud, reverb=1.0, echo=1.0, delay=1.0)
    assert (np.isfinite(out).all()) and (np.max(np.abs(out)) < 10.0)


def test_monitor_effect_amounts_are_continuous_and_disabled_slots_forget_stale_audio():
    low = MonitorEffectsChain._slots(0.01, 0.01, 0.01)
    high = MonitorEffectsChain._slots(0.02, 0.02, 0.02)
    assert all(0 < left[1] < right[1] for left, right in zip(low, high, strict=True))

    chain = MonitorEffectsChain(8000)
    impulse = np.zeros(100, dtype=np.float32)
    impulse[0] = 1.0
    chain.process(impulse, reverb=0.0, echo=1.0, delay=0.0)
    chain.process(np.zeros(100, dtype=np.float32), reverb=0.0, echo=0.0, delay=0.0)
    restarted = chain.process(
        np.zeros(4000, dtype=np.float32), reverb=0.0, echo=1.0, delay=0.0
    )
    assert np.count_nonzero(restarted) == 0


def test_pitch_shifter_neutral_value_is_an_exact_zero_latency_bypass():
    shifter = RealtimePitchShifter(48_000)
    samples = np.linspace(-0.5, 0.5, 128, dtype=np.float32)

    result = shifter.process(samples, 0)

    assert result is samples
    assert np.array_equal(result, samples)


def test_pitch_shifter_effect_window_keeps_algorithmic_monitor_latency_below_8ms():
    shifter = RealtimePitchShifter(48_000)

    # The dual-head shifter reads at most half of its history window behind
    # the live input.  Leave margin inside the 10-16 ms end-to-end target for
    # capture/render scheduling and the rest of the realtime DSP chain.
    assert shifter._buffer_len / 48_000 / 2 <= 0.008


@pytest.mark.parametrize("sample_rate", [16_000, 44_100, 48_000])
def test_pitch_shifter_reports_conservative_algorithmic_latency(sample_rate):
    shifter = RealtimePitchShifter(sample_rate)

    assert shifter.latency_ms(0.0) == 0.0
    assert shifter.latency_ms(0.004) == 0.0
    assert shifter.latency_ms(-0.5) == pytest.approx(6.0, abs=0.07)
    assert shifter.latency_ms(0.5) == pytest.approx(6.0, abs=0.07)


@pytest.mark.parametrize("sample_rate", [16_000, 48_000])
@pytest.mark.parametrize("octave", [-0.5, 0.5])
def test_pitch_shifter_audible_impulse_arrives_within_4_5ms(sample_rate, octave):
    shifter = RealtimePitchShifter(sample_rate)
    impulse = np.zeros(int(sample_rate * 0.04), dtype=np.float32)
    impulse[0] = 1.0
    output = np.concatenate([
        shifter.process(impulse[offset:offset + 64], octave)
        for offset in range(0, len(impulse), 64)
    ])

    peak = int(np.argmax(np.abs(output)))
    assert np.max(np.abs(output)) > 0.1
    assert peak / sample_rate <= 0.0045


def test_pitch_shifter_accepts_both_octave_directions_and_keeps_block_shape():
    source = np.sin(np.linspace(0, 16 * np.pi, 4096)).astype(np.float32)
    high = RealtimePitchShifter(48_000).process(source, 1)
    low = RealtimePitchShifter(48_000).process(source, -1)

    assert high.shape == source.shape
    assert low.shape == source.shape
    assert np.isfinite(high).all()
    assert np.isfinite(low).all()


def _reference_pitch_blocks(sample_rate, blocks, octave):
    length = max(
        96,
        int(round(max(8_000.0, float(sample_rate)) * RealtimePitchShifter._WINDOW_SEC)),
    )
    buffer = np.zeros(length, dtype=np.float32)
    write_pos, phase = 0, 0.0
    ratio = 2.0**octave
    outputs = []
    for source in blocks:
        output = np.empty_like(source)
        for index, sample in enumerate(source):
            buffer[write_pos] = sample
            phase_a = (phase + 1.0) % 1.0
            phase_b = (phase_a + 0.5) % 1.0
            read_a = (write_pos - 1.0 - phase_a * (length - 2.0)) % length
            read_b = (write_pos - 1.0 - phase_b * (length - 2.0)) % length
            a0, b0 = int(read_a), int(read_b)
            frac_a, frac_b = read_a - a0, read_b - b0
            sample_a = buffer[a0] * (1.0 - frac_a) + buffer[(a0 + 1) % length] * frac_a
            sample_b = buffer[b0] * (1.0 - frac_b) + buffer[(b0 + 1) % length] * frac_b
            weight_a = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase_a)
            weight_b = 0.5 - 0.5 * math.cos(2.0 * math.pi * phase_b)
            output[index] = (
                sample_a * weight_a + sample_b * weight_b
            ) / max(1e-6, weight_a + weight_b)
            phase = (phase + (1.0 - ratio) / length + 1.0) % 1.0
            write_pos = (write_pos + 1) % length
        outputs.append(output)
    return outputs


def test_vectorized_pitch_shifter_matches_original_streaming_algorithm():
    rng = np.random.default_rng(2026)
    blocks = [rng.uniform(-0.7, 0.7, size=size).astype(np.float32)
              for size in (64, 128, 256, 91)]
    for octave in (-0.75, 0.4, 1.0):
        expected = _reference_pitch_blocks(48_000, blocks, octave)
        shifter = RealtimePitchShifter(48_000)
        actual = [shifter.process(block, octave) for block in blocks]
        for reference, vectorized in zip(expected, actual, strict=True):
            np.testing.assert_allclose(vectorized, reference, rtol=2e-5, atol=2e-6)
