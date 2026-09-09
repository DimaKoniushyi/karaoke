from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from .errors import ConfigurationError


def _value(name: str, default, cast):
    try:
        return cast(os.getenv(name, default))
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"Invalid {name}") from error


@dataclass(frozen=True, slots=True)
class CoreConfig:
    sample_rate: int = 44100
    pitch_sample_rate: int = 16000
    hop_seconds: float = 0.01
    fmin_hz: float = 55.0
    fmax_hz: float = 1400.0
    min_note_sec: float = 0.07
    min_voiced_confidence: float = 0.38
    split_note_semitones: float = 0.78
    max_gap_sec: float = 0.08
    allow_fallback: bool = False
    validate_cached_artifacts: bool = True
    # How many songs AICoreService will run through the pipeline (or
    # analyze_pitch) at once. Kept modest by default since concurrent jobs
    # share GPU memory/compute -- raise it only on hardware known to have
    # headroom for more than one model resident at a time.
    max_concurrent_jobs: int = 1

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.pitch_sample_rate <= 0:
            raise ConfigurationError("Sample rates must be positive")
        if not 0 < self.fmin_hz < self.fmax_hz <= self.pitch_sample_rate / 2:
            raise ConfigurationError("Invalid pitch range")
        if not 0 <= self.min_voiced_confidence <= 1 or self.min_note_sec <= 0:
            raise ConfigurationError("Invalid melody thresholds")
        if self.max_concurrent_jobs < 1:
            raise ConfigurationError("max_concurrent_jobs must be at least 1")

    @classmethod
    def from_env(cls) -> CoreConfig:
        return cls(
            sample_rate=_value("KARAOKE_AI_SAMPLE_RATE", 44100, int),
            pitch_sample_rate=_value("KARAOKE_AI_PITCH_SR", 16000, int),
            hop_seconds=_value("KARAOKE_AI_HOP_SECONDS", 0.01, float),
            fmin_hz=_value("KARAOKE_AI_FMIN_HZ", 55.0, float),
            fmax_hz=_value("KARAOKE_AI_FMAX_HZ", 1400.0, float),
            min_note_sec=_value("KARAOKE_AI_MIN_NOTE_SEC", 0.07, float),
            min_voiced_confidence=_value("KARAOKE_AI_MIN_CONFIDENCE", 0.38, float),
            split_note_semitones=_value("KARAOKE_AI_SPLIT_SEMITONES", 0.78, float),
            max_gap_sec=_value("KARAOKE_AI_MAX_GAP_SEC", 0.08, float),
            allow_fallback=os.getenv("KARAOKE_AI_ALLOW_FALLBACK", "0").lower() in {"1", "true"},
            max_concurrent_jobs=_value("KARAOKE_AI_MAX_CONCURRENT_JOBS", 1, int),
        )

    def fingerprint(self) -> dict[str, object]:
        return asdict(self)
