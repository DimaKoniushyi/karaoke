from __future__ import annotations

import numpy as np

from ..audio import load_mono
from ..errors import EngineUnavailableError
from ..models import PitchFrame
from .base import PitchEstimator
from .device import select_torch_device


def _prepare_model_signal(signal) -> np.ndarray:
    """Return finite FCPE input inside [-1, 1] without hard clipping peaks."""
    prepared = np.nan_to_num(
        np.asarray(signal, dtype=np.float32),
        copy=True,
        nan=0.0,
        posinf=1.0,
        neginf=-1.0,
    )
    peak = float(np.max(np.abs(prepared))) if prepared.size else 0.0
    if peak > 1.0:
        prepared /= peak
    return prepared


def _frames_from_frequencies(frequencies, step: float, fmin: float, fmax: float) -> list[PitchFrame]:
    """Build one PitchFrame per inference frame, masking out-of-range pitches.

    The fmin/fmax range check and value/confidence masking are vectorized
    once over every frame (thousands per song) instead of being
    re-evaluated three times per frame inside a Python-level list
    comprehension; only building the immutable PitchFrame records
    themselves stays a loop.
    """
    frequencies = np.asarray(frequencies)
    times = np.arange(len(frequencies)) * step
    voiced = (frequencies >= fmin) & (frequencies <= fmax)
    values = np.where(voiced, frequencies, 0.0)
    return [
        PitchFrame(float(time), float(value), float(is_voiced), bool(is_voiced))
        for time, value, is_voiced in zip(times, values, voiced, strict=True)
    ]


class FCPEPitchEstimator(PitchEstimator):
    name = "fcpe"

    def __init__(self, sr=16000, hop=160, fmin=55.0, fmax=1400.0):
        self.sr, self.hop, self.fmin, self.fmax = int(sr), int(hop), float(fmin), float(fmax)
        self._model = self._device = None

    def _load(self):
        try:
            import torch
            import torchfcpe
        except ImportError as error:
            raise EngineUnavailableError("torchfcpe is unavailable") from error
        if self._model is None:
            self._device = select_torch_device(torch, "pitch")
            self._model = torchfcpe.spawn_bundled_infer_model(device=self._device)
            self._model.eval()
        else:
            device = select_torch_device(torch, "pitch")
            if device != self._device:
                self._model.to(device)
                self._device = device
                self._model.eval()
        return torch, self._model

    def estimate(self, audio):
        torch, model = self._load()
        signal, _ = load_mono(audio, self.sr)
        if not signal.size:
            return []
        signal = _prepare_model_signal(signal)
        tensor = torch.from_numpy(signal).view(1, -1, 1).to(self._device)
        with torch.inference_mode():
            raw = model.infer(tensor, sr=self.sr, decoder_mode="local_argmax", threshold=0.006)
        frequencies = np.asarray((raw[0] if isinstance(raw, (tuple, list)) else raw).squeeze().cpu())
        step = len(signal) / self.sr / max(1, len(frequencies))
        return _frames_from_frequencies(frequencies, step, self.fmin, self.fmax)

    def close(self) -> None:
        self._model = self._device = None

    def park(self) -> None:
        if self._model is not None:
            self._model.to("cpu")
            self._device = "cpu"


class PyinFallbackPitchEstimator(FCPEPitchEstimator):
    name = "pyin"
