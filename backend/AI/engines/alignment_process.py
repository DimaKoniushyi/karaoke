from __future__ import annotations

import contextlib
import multiprocessing
import os
import threading
import time
import traceback
from queue import Empty

from ..errors import (
    AlignmentTimeoutError,
    EngineUnavailableError,
    InvalidArtifactError,
    ProcessingCancelledError,
)


def alignment_timeout_seconds() -> float:
    """Return a bounded deadline so a wedged model cannot hold a song forever."""
    return max(30.0, float(os.getenv("KARAOKE_AI_ALIGN_TIMEOUT_SECONDS", "60")))


def configure_worker_resource_limits(device: str) -> None:
    """Keep an inference child from starving the Windows desktop."""
    for variable in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS"):
        os.environ[variable] = "2"
    try:
        import torch

        torch.set_num_threads(2)
        if device == "cuda":
            try:
                fraction = float(os.getenv("KARAOKE_AI_ALIGN_GPU_MEMORY_FRACTION", "0.5"))
            except ValueError:
                fraction = 0.5
            torch.cuda.set_per_process_memory_fraction(min(0.75, max(0.25, fraction)))
    except (ImportError, RuntimeError, AttributeError):
        pass


def _worker(model_name, method, arguments, results, parent_pid, device):
    try:
        try:
            import psutil

            process = psutil.Process()
            with contextlib.suppress(psutil.Error, AttributeError):
                process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS if os.name == "nt" else 10)

            def stop_with_parent():
                while psutil.pid_exists(parent_pid):
                    time.sleep(1)
                os._exit(1)

            threading.Thread(target=stop_with_parent, daemon=True).start()
        except (ImportError, OSError):
            pass
        from ..runtime import configure_runtime
        from .text import Qwen3ForcedAligner

        configure_worker_resource_limits(device)
        configure_runtime(device, force=True)
        aligner = Qwen3ForcedAligner(model_name, isolated=False)
        words = getattr(aligner, method)(*arguments)
        results.put(("ok", words, aligner.needs_voice_anchoring))
    except BaseException as error:
        results.put(("error", type(error).__name__, str(error), traceback.format_exc()))


class IsolatedAlignmentProcess:
    def __init__(self, model_name, cancelled=None):
        self.model_name = model_name
        self.cancelled = cancelled
        self.process = self.results = None

    def close(self):
        process, results = self.process, self.results
        self.process = self.results = None
        if process and process.is_alive():
            process.terminate()
            process.join(timeout=2)
        if process and process.is_alive() and hasattr(process, "kill"):
            process.kill()
            process.join(timeout=1)
        if results is not None:
            with contextlib.suppress(AttributeError, OSError, ValueError):
                results.close()
                results.cancel_join_thread()

    def run(self, method: str, *arguments):
        if callable(self.cancelled) and self.cancelled():
            raise ProcessingCancelledError("Song processing cancelled")
        from ..runtime import selected_backend

        backend = selected_backend("aligner")
        device = backend.device if backend is not None else "cpu"
        context = multiprocessing.get_context("spawn")
        self.results = context.Queue(1)
        self.process = context.Process(
            target=_worker,
            args=(self.model_name, method, arguments, self.results, os.getpid(), device),
            daemon=True,
        )
        self.process.start()
        timeout = alignment_timeout_seconds()
        deadline = time.monotonic() + timeout
        try:
            while True:
                if callable(self.cancelled) and self.cancelled():
                    raise ProcessingCancelledError("Song processing cancelled")
                if time.monotonic() >= deadline:
                    raise AlignmentTimeoutError(
                        f"Forced alignment exceeded the {timeout:g}-second limit"
                    )
                try:
                    response = self.results.get(timeout=0.25)
                except Empty:
                    if not self.process.is_alive():
                        raise EngineUnavailableError(
                            f"Forced alignment worker exited with {self.process.exitcode}"
                        ) from None
                    continue
                if response[0] == "ok":
                    return response[1], bool(response[2])
                _status, error_type, message, details = response
                print(details, flush=True)
                error = (
                    InvalidArtifactError
                    if error_type == "InvalidArtifactError"
                    else EngineUnavailableError
                )
                raise error(message or error_type)
        finally:
            self.close()


class IsolatedAlignerMixin:
    def _configure_isolation(self, model_name: str, isolated: bool) -> None:
        self._isolation_model_name = model_name
        self._isolated = isolated
        self._cancelled = None
        self._worker = None

    def set_cancelled(self, callback) -> None:
        self._cancelled = callback

    def _ensure_alignment_backend(self, resolved: str) -> bool:
        if callable(self._cancelled) and self._cancelled():
            raise ProcessingCancelledError("Song processing cancelled")
        heavy = self._heavy_alignment_enabled()
        variable = {
            "Russian": "KARAOKE_AI_CTC_RU_MODEL",
            "Ukrainian": "KARAOKE_AI_CTC_UK_MODEL",
        }.get(resolved)
        print(
            f"[AI] alignment route language={resolved} heavy_allowed={heavy} "
            f"ctc_available={bool(variable and os.getenv(variable))}",
            flush=True,
        )
        return bool(heavy or (variable and os.getenv(variable)))

    def _heavy_alignment_enabled(self) -> bool:
        return self._isolation_model_name == "test-model" or os.getenv(
            "KARAOKE_AI_ENABLE_HEAVY_ALIGNER", "0"
        ).lower() in {"1", "true", "yes"}

    def _ensure_heavy_alignment(self) -> None:
        if not self._heavy_alignment_enabled():
            raise EngineUnavailableError(
                "Heavy Qwen forced alignment is disabled for system safety"
            )

    def _stop_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None:
            worker.close()

    def _run_isolated(self, method: str, *arguments):
        if not self._isolated:
            return getattr(self, f"_{method}_local")(*arguments)
        self._worker = IsolatedAlignmentProcess(self._isolation_model_name, self._cancelled)
        try:
            words, anchoring = self._worker.run(method, *arguments)
            self.needs_voice_anchoring = anchoring
            return words
        finally:
            self._stop_worker()
