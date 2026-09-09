from __future__ import annotations

import contextlib
import multiprocessing
import queue
from pathlib import Path


def _process_entry(
    results,
    title: str,
    artist: str | None,
    output_dir: str,
    expected_duration: float | None,
) -> None:
    from app.services.metadata_enrichment_service import prepare_training_media

    try:
        payload = prepare_training_media(
            title,
            artist,
            Path(output_dir),
            expected_duration=expected_duration,
        )
        results.put(("ok", payload))
    except BaseException as exc:  # child-process boundary
        results.put(("error", f"{type(exc).__name__}: {exc}"))


class TrainingMediaProcess:
    def __init__(
        self,
        title: str,
        artist: str | None,
        output_dir: Path,
        *,
        expected_duration: float | None = None,
    ) -> None:
        context = multiprocessing.get_context("spawn")
        self._results = context.Queue(1)
        self._process = context.Process(
            target=_process_entry,
            args=(self._results, title, artist, str(output_dir), expected_duration),
            daemon=True,
            name="audio-v2-media",
        )
        self._process.start()

    @property
    def pid(self) -> int | None:
        return self._process.pid

    def result(self, *, cancelled=None) -> dict[str, object]:
        while True:
            if callable(cancelled) and cancelled():
                self.close()
                raise RuntimeError("Media preparation cancelled")
            try:
                status, payload = self._results.get(timeout=0.25)
            except queue.Empty:
                if self._process.is_alive():
                    continue
                raise RuntimeError(
                    f"Media preparation process exited with {self._process.exitcode}"
                ) from None
            self._process.join(timeout=1)
            if status != "ok":
                raise RuntimeError(str(payload))
            return dict(payload)

    def close(self) -> None:
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=2)
        if self._process.is_alive() and hasattr(self._process, "kill"):
            self._process.kill()
            self._process.join(timeout=2)
        with contextlib.suppress(OSError, ValueError):
            self._results.close()
            self._results.cancel_join_thread()


def start(
    title: str,
    artist: str | None,
    output_dir: Path,
    *,
    expected_duration: float | None = None,
) -> TrainingMediaProcess:
    return TrainingMediaProcess(
        title,
        artist,
        output_dir,
        expected_duration=expected_duration,
    )
