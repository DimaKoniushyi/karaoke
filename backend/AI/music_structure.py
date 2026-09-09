from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True, slots=True)
class SectionReprise:
    start: float
    end: float
    similarity: float
    tempo_ratio: float


def extract_music_structure(path) -> tuple[np.ndarray, float, float]:
    """Extract compact harmony/timbre features with one decoded audio pass."""
    import librosa

    hop = 1024
    audio, rate = librosa.load(path, sr=11_025, mono=True)
    if not audio.size:
        return np.zeros((20, 0), dtype=np.float32), rate / hop, 0.0
    harmonic = librosa.effects.harmonic(audio)
    chroma = librosa.feature.chroma_cqt(y=harmonic, sr=rate, hop_length=hop)
    mfcc = librosa.feature.mfcc(y=audio, sr=rate, hop_length=hop, n_mfcc=8)
    frames = min(chroma.shape[1], mfcc.shape[1])
    matrix = np.vstack((chroma[:, :frames], mfcc[:, :frames])).astype(np.float32)
    matrix -= np.median(matrix, axis=1, keepdims=True)
    scale = np.quantile(np.abs(matrix), 0.9, axis=1, keepdims=True)
    matrix /= np.maximum(scale, 1e-6)
    return matrix, rate / hop, len(audio) / rate


def _normalized(values: np.ndarray) -> np.ndarray:
    centered = values - values.mean(axis=1, keepdims=True)
    scale = float(np.linalg.norm(centered))
    return centered.reshape(-1) / max(scale, 1e-9)


def _resample_frames(values: np.ndarray, size: int) -> np.ndarray:
    if values.shape[1] == size:
        return values
    source = np.linspace(0.0, 1.0, values.shape[1])
    target = np.linspace(0.0, 1.0, size)
    return np.vstack([np.interp(target, source, row) for row in values])


def find_section_reprise(
    features: np.ndarray,
    *,
    frames_per_second: float,
    template_start: float,
    template_end: float,
    search_start: float,
    search_end: float,
    minimum_similarity: float = 0.78,
) -> SectionReprise | None:
    """Find a later occurrence of a musical section with modest tempo drift."""
    matrix = np.asarray(features, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] < 4 or frames_per_second <= 0:
        return None
    lower = max(0, round(template_start * frames_per_second))
    upper = min(matrix.shape[1], round(template_end * frames_per_second))
    if upper - lower < 4:
        return None
    template = matrix[:, lower:upper]
    reference = _normalized(template)
    search_lower = max(0, round(search_start * frames_per_second))
    search_upper = min(matrix.shape[1], round(search_end * frames_per_second))
    best: SectionReprise | None = None
    for ratio in (0.9, 0.95, 1.0, 1.05, 1.1):
        width = max(4, round(template.shape[1] * ratio))
        for start in range(search_lower, search_upper - width + 1):
            candidate = _resample_frames(matrix[:, start : start + width], template.shape[1])
            similarity = float(reference @ _normalized(candidate))
            if best is None or similarity > best.similarity:
                best = SectionReprise(
                    start / frames_per_second,
                    (start + width) / frames_per_second,
                    similarity,
                    ratio,
                )
    if best is None or best.similarity < minimum_similarity:
        return None
    return best


def find_partial_section_reprise(
    features: np.ndarray,
    *,
    frames_per_second: float,
    template_start: float,
    template_end: float,
    search_start: float,
    search_end: float,
    minimum_similarity: float = 0.4,
    start_tolerance_seconds: float = 2.0,
) -> SectionReprise | None:
    """Find a truncated reprise only when independent short windows agree."""
    matrix = np.asarray(features, dtype=np.float32)
    if matrix.ndim != 2:
        return None
    harmonic = matrix[:12] if matrix.shape[0] >= 12 else matrix
    if template_end - template_start < 12.0:
        return None
    matches = [
        find_section_reprise(
            harmonic,
            frames_per_second=frames_per_second,
            template_start=template_start,
            template_end=template_start + seconds,
            search_start=search_start,
            search_end=search_end,
            minimum_similarity=minimum_similarity,
        )
        for seconds in (8.0, 12.0)
    ]
    if any(match is None for match in matches):
        return None
    short, long = matches
    if abs(short.start - long.start) > start_tolerance_seconds:
        return None
    return long
