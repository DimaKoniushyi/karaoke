from __future__ import annotations

import threading

from .audio_pipeline_v2 import (
    AudioPipelineV2,
    AudioPipelineV2Request,
    AudioPipelineV2Result,
)
from .config import CoreConfig
from .engines.autocorrelation_pitch import AutocorrelationPitchEstimator
from .errors import ConfigurationError
from .pitch_post import stabilize_pitch
from .runtime import get_runtime_plan


class AICoreService:
    def __init__(self, config: CoreConfig | None = None):
        self.config = config or CoreConfig.from_env()
        # Ordinary audio uploads and melody reprocessing share the same clean
        # audio-v2 implementation.  Reprocessing must never route an already
        # correct package back through the legacy karaoke pipeline.
        self.pipeline = AudioPipelineV2(self.config)
        # Deliberately separate from self.pipeline.engines.pitch (FCPE): that
        # instance also builds every song's reference melody, a much larger
        # blast radius than scoring one user's recording. Only analyze_pitch
        # uses this lightweight estimator, matching the live JS UI meter.
        self._recording_pitch = AutocorrelationPitchEstimator(sr=self.config.pitch_sample_rate)
        # A plain lock capped concurrent AI work at exactly one job, even
        # across unrelated songs (and users) that had nothing to serialize
        # for -- a GPU-bound stage on one song blocked a purely CPU-bound
        # stage on another. A semaphore allows up to max_concurrent_jobs at
        # once instead, bounded so concurrent jobs don't exceed available
        # GPU memory/compute.
        self._max_concurrent = self.config.max_concurrent_jobs
        self._lock = threading.Semaphore(self._max_concurrent)

    def process_song(self, source_path, output_dir, **options) -> AudioPipelineV2Result:
        with self._lock:
            return self.pipeline.run(
                AudioPipelineV2Request(source_path, output_dir, **options)
            )

    def reprocess_song(self, output_dir, **options) -> AudioPipelineV2Result:
        with self._lock:
            return self.pipeline.reprocess_song(output_dir, **options)

    def analyze_pitch(self, audio_path):
        with self._lock:
            return stabilize_pitch(self._recording_pitch.estimate(audio_path))

    def separate_stems(
        self,
        source_path,
        vocals_path,
        instrumental_path,
        *,
        processing_mode="fast",
    ) -> None:
        with self._lock:
            self.pipeline.separate_stems(
                source_path,
                vocals_path,
                instrumental_path,
                processing_mode=processing_mode,
            )

    def close(self):
        # Closing releases shared model resources (see pipeline.close()), so
        # it must never run while a job is mid-flight -- acquiring every
        # permit blocks until all currently-running jobs finish and holds
        # off any new ones from starting in the meantime.
        for _ in range(self._max_concurrent):
            self._lock.acquire()
        try:
            self.pipeline.close()
        finally:
            for _ in range(self._max_concurrent):
                self._lock.release()

    def health(self) -> dict:
        engines = self.pipeline.engines
        return {
            "version": self.pipeline.VERSION,
            "separator": engines.separator.name,
            "pitch": engines.pitch.name,
            "recording_pitch": self._recording_pitch.name,
            "transcriber": engines.transcriber.name,
            "aligner": engines.aligner.name,
            "runtime": get_runtime_plan().describe(),
        }


_service = None
_config = None
_lock = threading.Lock()


def get_ai_service(config: CoreConfig | None = None) -> AICoreService:
    global _service, _config
    requested = config or CoreConfig.from_env()
    with _lock:
        if _service is None:
            _service, _config = AICoreService(requested), requested
        elif _config != requested:
            raise ConfigurationError("AI service is already configured")
        return _service


def reset_ai_service() -> None:
    global _service, _config
    with _lock:
        if _service:
            _service.close()
        _service = _config = None


def process_song(*args, **kwargs):
    return get_ai_service().process_song(*args, **kwargs)
