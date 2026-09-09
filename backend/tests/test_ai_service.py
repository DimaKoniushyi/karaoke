import threading
import time
from unittest.mock import Mock

from AI.config import CoreConfig
from AI.service import AICoreService


class BlockingPipeline:
    """A fake KaraokePipeline whose run()/reprocess() block until released,
    so tests can observe how many calls AICoreService lets through at once.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.proceed = threading.Event()
        self.close_calls = 0

    def _blocking_call(self):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.proceed.wait(timeout=5)
        with self.lock:
            self.active -= 1
        return "done"

    def run(self, _request):
        return self._blocking_call()

    def reprocess(self, _output_dir, **_options):
        return self._blocking_call()

    def close(self):
        self.close_calls += 1


def make_service(pipeline, max_concurrent):
    service = AICoreService.__new__(AICoreService)
    service.config = CoreConfig(max_concurrent_jobs=max_concurrent)
    service.pipeline = pipeline
    service._max_concurrent = max_concurrent
    service._lock = threading.Semaphore(max_concurrent)
    return service


def test_process_song_allows_up_to_max_concurrent_jobs_at_once():
    pipeline = BlockingPipeline()
    service = make_service(pipeline, max_concurrent=2)

    threads = [
        threading.Thread(
            target=service.process_song,
            args=(f"source-{i}", f"out-{i}"),
            kwargs={"artist": "Artist", "title": f"Song {i}"},
        )
        for i in range(3)
    ]
    for thread in threads:
        thread.start()
    # Give all three a chance to reach the blocking call; only two should
    # actually get past the semaphore into the pipeline at once.
    time.sleep(0.2)
    assert pipeline.active == 2

    pipeline.proceed.set()
    for thread in threads:
        thread.join(timeout=5)
    assert pipeline.max_active == 2
    assert pipeline.active == 0


def test_reprocess_song_stays_on_audio_v2_and_never_calls_legacy_pipeline(tmp_path):
    pipeline = Mock()
    pipeline.reprocess_song.return_value = "new-result"
    legacy = Mock()
    service = make_service(pipeline, max_concurrent=1)
    service._reprocessor = legacy

    result = service.reprocess_song(tmp_path, language="Russian")

    assert result == "new-result"
    pipeline.reprocess_song.assert_called_once_with(tmp_path, language="Russian")
    legacy.reprocess.assert_not_called()


def test_close_waits_for_in_flight_jobs_and_blocks_new_ones_meanwhile():
    pipeline = BlockingPipeline()
    service = make_service(pipeline, max_concurrent=2)

    job_threads = [
        threading.Thread(
            target=service.process_song,
            args=(f"source-{i}", f"out-{i}"),
            kwargs={"artist": "Artist", "title": f"Song {i}"},
        )
        for i in range(2)
    ]
    for thread in job_threads:
        thread.start()
    time.sleep(0.2)
    assert pipeline.active == 2

    close_finished = threading.Event()

    def do_close():
        service.close()
        close_finished.set()

    close_thread = threading.Thread(target=do_close)
    close_thread.start()
    time.sleep(0.2)
    # close() needs every permit, held by the two in-flight jobs -- it must
    # still be waiting, and a new job must not be able to sneak in either.
    assert not close_finished.is_set()
    assert pipeline.close_calls == 0
    new_job_started = threading.Event()
    new_job_result = {}

    def new_job():
        new_job_started.set()
        new_job_result["ran"] = service.process_song(
            "late-source", "late-out", artist="Artist", title="Late song"
        )

    new_job_thread = threading.Thread(target=new_job)
    new_job_thread.start()
    assert new_job_started.wait(timeout=2)
    time.sleep(0.2)
    assert pipeline.active == 2  # the late job is still blocked behind close()

    pipeline.proceed.set()
    close_thread.join(timeout=5)
    new_job_thread.join(timeout=5)

    assert close_finished.is_set()
    assert pipeline.close_calls == 1
    assert new_job_result["ran"] == "done"
