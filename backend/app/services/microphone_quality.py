from __future__ import annotations

import math

import numpy as np
from scipy.signal import lfilter, lfiltic

# Largest float32 value that is guaranteed not to exceed the documented 0.985
# limiter ceiling when compared in Python/float64. ``np.float32(0.985)`` itself
# is slightly larger (0.985000014...), which made exact limiter hits violate
# the public bound on some tests/platforms.
_LIMITER_CEILING = np.nextafter(np.float32(0.985), np.float32(0.0))


def clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


class StudioMicrophoneProcessor:

    def __init__(self, sample_rate: float, channels: int = 1):
        self.sample_rate = max(8_000.0, float(sample_rate))
        self.channels = max(1, int(channels))
        self._gate_gain = 1.0
        self._compressor_gain = 1.0
        self._noise_floor = 0.003
        self._hp_r = math.exp(-2.0 * math.pi * 70.0 / self.sample_rate)
        self._tone_alpha = 1.0 - \
            math.exp(-2.0 * math.pi * 2200.0 / self.sample_rate)
        # hp[n] = x[n] - x[n-1] + hp_r*hp[n-1] and tone_low[n] =
        # tone_alpha*hp[n] + (1-tone_alpha)*tone_low[n-1] are both first-order
        # IIR recursions, previously run one sample at a time in a Python
        # loop on every microphone callback buffer. scipy.signal.lfilter
        # applies the same recursion to a whole buffer in one call (axis=0
        # keeps each channel independent); zi/zf carry the filter's state
        # across buffers exactly like _prev_x/_hp_y/_tone_low did before.
        self._hp_b, self._hp_a = np.array([1.0, -1.0]), np.array([1.0, -self._hp_r])
        self._tone_b = np.array([self._tone_alpha])
        self._tone_a = np.array([1.0, -(1.0 - self._tone_alpha)])
        self._reset_filter_state()

    def _reset_filter_state(self) -> None:
        hp_zi = lfiltic(self._hp_b, self._hp_a, y=[0.0], x=[0.0])
        tone_zi = lfiltic(self._tone_b, self._tone_a, y=[0.0], x=[0.0])
        self._hp_zi = np.tile(hp_zi[:, None], (1, self.channels))
        self._tone_zi = np.tile(tone_zi[:, None], (1, self.channels))

    def process(self, data, gain: float = 1.0, noise_suppression: float = 0.35):
        source = np.asarray(data, dtype=np.float32)
        if source.ndim == 1:
            source = source[:, None]
        if source.size == 0:
            return source.copy()
        if source.shape[1] != self.channels:
            self.channels = source.shape[1]
            self._reset_filter_state()

        x = np.clip(source * float(gain), -1.5, 1.5).astype(np.float64)
        hp, self._hp_zi = lfilter(self._hp_b, self._hp_a, x, axis=0, zi=self._hp_zi)
        tone_low, self._tone_zi = lfilter(self._tone_b, self._tone_a, hp, axis=0, zi=self._tone_zi)
        y = hp * 0.94 + (hp - tone_low) * 0.16

        rms = float(np.sqrt(np.mean(np.square(y), dtype=np.float64)))
        if rms < self._noise_floor * 1.4:
            self._noise_floor = self._noise_floor * 0.995 + rms * 0.005
        strength = clamp01(noise_suppression)
        threshold = max(0.0025, self._noise_floor * (2.4 + strength * 1.2))
        if rms <= threshold:
            closed_gain = 0.12 - strength * 0.08
            target_gate = closed_gain + (0.5 - closed_gain) * (
                rms / threshold if threshold else 0.0
            )
        elif rms < threshold * 2.2:
            target_gate = 0.5 + 0.5 * (rms - threshold) / (threshold * 1.2)
        else:
            target_gate = 1.0
        target_gate = 1.0 + (target_gate - 1.0) * strength
        gate_speed = 0.55 if target_gate > self._gate_gain else 0.18
        self._gate_gain += (target_gate - self._gate_gain) * gate_speed
        y *= self._gate_gain

        rms, threshold_comp = float(
            np.sqrt(np.mean(np.square(y), dtype=np.float64))), 0.16
        if rms > threshold_comp:
            compressed = threshold_comp * (rms / threshold_comp) ** (1.0 / 3.0)
            target_comp = compressed / max(rms, 1e-9)
        else:
            target_comp = 1.0
        comp_speed = 0.35 if target_comp < self._compressor_gain else 0.08
        self._compressor_gain += (target_comp -
                                  self._compressor_gain) * comp_speed
        y *= self._compressor_gain * 1.08

        y = np.tanh(y * 1.12) / np.tanh(1.12)
        return np.clip(y, -_LIMITER_CEILING, _LIMITER_CEILING).astype(np.float32, copy=False)


class RealtimePitchShifter:
    """Streaming pitch shift with a true zero-latency neutral bypass."""

    _WINDOW_SEC = 0.012

    def __init__(self, sample_rate: float):
        self._buffer_len = max(
            96,
            int(round(max(8_000.0, float(sample_rate)) * self._WINDOW_SEC)),
        )
        self._buffer = np.zeros(self._buffer_len, dtype=np.float32)
        self._write_pos = 0
        self._phase = 0.0
        self._previous_octave = 0.0

    def process(self, samples: np.ndarray, octave: float) -> np.ndarray:
        octave = max(-1.0, min(1.0, float(octave)))
        source = np.asarray(samples, dtype=np.float32)
        if abs(octave) < 0.005:
            if abs(self._previous_octave) >= 0.005:
                self._buffer.fill(0.0)
                self._phase = 0.0
            self._previous_octave = octave
            return source

        ratio = 2.0**octave
        length = self._buffer_len
        count = len(source)
        if not count:
            return np.empty_like(source)
        # The ring contains input samples only, so all fractional read heads
        # can be evaluated together against a chronological history followed
        # by this already-known input block. This preserves the original
        # causal interpolation without a Python loop in the realtime callback.
        history = np.concatenate((
            self._buffer[self._write_pos:],
            self._buffer[:self._write_pos],
            source,
        ))
        increment = (1.0 - ratio) / length
        phases = np.mod(self._phase + np.arange(count) * increment, 1.0)
        phase_a = phases
        phase_b = np.mod(phases + 0.5, 1.0)
        current = np.arange(count, dtype=np.float64)

        def read(phase):
            positions = length + current - 1.0 - phase * (length - 2.0)
            lower = np.floor(positions).astype(np.intp)
            fraction = positions - lower
            return history[lower] * (1.0 - fraction) + history[lower + 1] * fraction

        sample_a, sample_b = read(phase_a), read(phase_b)
        weight_a = 0.5 - 0.5 * np.cos(2.0 * math.pi * phase_a)
        weight_b = 0.5 - 0.5 * np.cos(2.0 * math.pi * phase_b)
        output = (sample_a * weight_a + sample_b * weight_b) / np.maximum(
            1e-6, weight_a + weight_b
        )
        if count >= length:
            self._buffer[:] = source[-length:]
            self._write_pos = 0
        else:
            positions = (self._write_pos + np.arange(count)) % length
            self._buffer[positions] = source
            self._write_pos = (self._write_pos + count) % length
        self._phase = (self._phase + count * increment) % 1.0
        self._previous_octave = octave
        return output.astype(source.dtype, copy=False)


class MonitorEffectsChain:

    _SLOT_COUNT = 6
    _MAX_DELAY_SEC = 0.6

    def __init__(self, sample_rate: float):
        self.sample_rate = max(8_000.0, float(sample_rate))
        self._buffer_len = int(self.sample_rate * self._MAX_DELAY_SEC) + 1
        self._buffers = np.zeros(
            (self._SLOT_COUNT, self._buffer_len), dtype=np.float32)
        self._write_pos = 0
        self._active_slots = [False] * self._SLOT_COUNT

    @staticmethod
    def _slots(reverb: float, echo: float, delay: float) -> tuple[tuple[float, float], ...]:
        reverb, echo, delay = clamp01(reverb), clamp01(echo), clamp01(delay)
        return (
            (0.055, reverb * 0.44),
            (0.110, reverb * 0.32),
            (0.170, reverb * 0.22),
            (0.180, echo * 0.56),
            (0.360, echo * 0.32),
            (0.110 + delay * 0.39, delay * 0.50),
        )

    def process(self, samples: np.ndarray, reverb: float, echo: float, delay: float) -> np.ndarray:
        slots = self._slots(reverb, echo, delay)
        active = [decay > 0.0 for _, decay in slots]
        for slot, enabled in enumerate(active):
            if self._active_slots[slot] and not enabled:
                self._buffers[slot].fill(0.0)
        self._active_slots = active
        if not any(active):
            return samples
        length = self._buffer_len
        offsets = [min(length - 1, max(1, int(delay_sec * self.sample_rate))) for delay_sec, _ in slots]
        decays = [decay for _, decay in slots]
        count = len(samples)
        if count and all(offset >= count for slot, offset in enumerate(offsets) if active[slot]):
            return self._process_block(samples, active, offsets, decays, count)
        return self._process_sample_by_sample(samples, active, offsets, decays, length)

    def _process_block(self, samples, active, offsets, decays, count) -> np.ndarray:
        # Live monitoring calls this on every audio callback -- at the small
        # (64-256 sample) blocksizes real-time low-latency monitoring uses,
        # that can be several hundred times a second. Every configured tap
        # delay is comfortably longer than one callback's worth of samples
        # (the shortest is ~0.055s, versus a 256-sample block being ~5ms at
        # 48kHz), so a whole block's reads always land in already-settled
        # buffer content from earlier calls, never in samples this same call
        # is about to write -- which means the block can be gathered/
        # scattered in one vectorized pass per slot instead of one Python
        # step per sample.
        length = self._buffer_len
        dry = samples.astype(np.float64, copy=False)
        wet = np.zeros(count, dtype=np.float64)
        write_positions = (self._write_pos + np.arange(count)) % length
        for slot in range(self._SLOT_COUNT):
            if not active[slot]:
                continue
            read_positions = (write_positions - offsets[slot]) % length
            delayed = self._buffers[slot, read_positions]
            self._buffers[slot, write_positions] = dry + decays[slot] * delayed
            wet += decays[slot] * delayed
        self._write_pos = (self._write_pos + count) % length
        return (dry + wet).astype(samples.dtype, copy=False)

    def _process_sample_by_sample(self, samples, active, offsets, decays, length) -> np.ndarray:
        # Fallback for a tap delay shorter than the current block (not
        # reachable with any real audio callback size, only exercised by
        # deliberately extreme test parameters) -- samples within the block
        # can then depend on each other through the same slot's buffer, so
        # they must be resolved one at a time.
        buffers, write_pos, out = self._buffers, self._write_pos, np.empty_like(samples)
        for index in range(len(samples)):
            dry = float(samples[index])
            wet = 0.0
            for slot in range(self._SLOT_COUNT):
                if not active[slot]:
                    continue
                read_pos = (write_pos - offsets[slot]) % length
                delayed = buffers[slot, read_pos]
                buffers[slot, write_pos] = dry + decays[slot] * delayed
                wet += decays[slot] * delayed
            out[index] = dry + wet
            write_pos = (write_pos + 1) % length
        self._write_pos = write_pos
        return out
