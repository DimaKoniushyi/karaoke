import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

from app.services import metadata_enrichment_service as metadata
from app.services import metadata_media_process
from tests._shared import patch_attrs, raises


def test_itunes_lookup_returns_the_first_catalog_genre(monkeypatch):
    payload = {"results": [{"primaryGenreName": "Rock"}, {"primaryGenreName": "Pop"}]}
    request = Mock(return_value=json.dumps(payload).encode())
    monkeypatch.setattr(metadata, "_request", request)

    assert metadata._itunes_genre("Song", "Artist") == "Rock"
    assert "entity=song" in request.call_args.args[0]


def test_youtube_lookup_uses_keyless_embeddable_result(monkeypatch):
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    request = Mock(return_value=b'ignored {"videoId":"DAaLa3vF8sU"} ignored')
    monkeypatch.setattr(metadata, "_request", request)
    monkeypatch.setattr(metadata, "_youtube_oembed", Mock(return_value={
        "title": "Artist - 8th Color (Official Music Video)",
        "author_name": "Artist",
    }))
    monkeypatch.setattr(metadata, "_youtube_has_motion", Mock(return_value=True))

    assert metadata._youtube_video_id("8th Color", "Artist") == "DAaLa3vF8sU"
    search_url = request.call_args_list[0].args[0]
    assert "official+video" in search_url
    assert "official+music+video" not in search_url


def test_youtube_quality_rejects_static_and_low_quality_candidates(monkeypatch):
    monkeypatch.setattr(metadata, "_youtube_oembed", Mock(return_value={
        "title": "Artist - Song (Official Music Video)",
        "author_name": "Artist",
    }))
    monkeypatch.setattr(metadata, "_youtube_has_motion", Mock(return_value=False))
    assert metadata._youtube_video_is_acceptable("DAaLa3vF8sU", "Song", "Artist") is False

    monkeypatch.setattr(metadata, "_youtube_has_motion", Mock(return_value=True))
    metadata._youtube_oembed.return_value["title"] = "Artist - Song (Official Audio)"
    assert metadata._youtube_video_is_acceptable("DAaLa3vF8sU", "Song", "Artist") is False


def test_youtube_quality_keeps_live_and_fan_clips_as_ranked_fallbacks_but_rejects_ai():
    live = metadata._candidate_score(
        "Artist - Song (Live in Concert)", "Artist", "Song", "Artist"
    )
    fan = metadata._candidate_score(
        "Artist - Song | Фан-клип с оригинальным сюжетом", "Artist", "Song", "Artist"
    )
    plain = metadata._candidate_score("Artist - Song", "Artist", "Song", "Artist")
    assert live is not None and fan is not None and plain is not None
    assert live < plain and fan < plain
    assert metadata._candidate_score(
        "Artist - Song (Нейросеть AI Generated Music Video)", "Artist", "Song", "Artist"
    ) is None


def test_official_video_scores_above_an_unverified_clip():
    official = metadata._candidate_score(
        "Artist - Song (Official Video)", "Artist", "Song", "Artist"
    )
    generic = metadata._candidate_score(
        "Artist - Song (Клип 2024)", "Artist", "Song", "Artist"
    )
    assert official is not None and generic is not None and official > generic


def test_video_normalization_accepts_normal_video_intro_duration(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(metadata, "_video_info", Mock(return_value=(1920, 1080, 206.0)))
    monkeypatch.setattr(metadata, "_video_has_motion", Mock(return_value=True))
    monkeypatch.setattr(metadata, "_detected_crop", Mock(return_value="crop=1920:1080:0:0"))

    # Let the fake encoder publish a sufficiently large destination.
    def encoded(*_args, **_kwargs):
        temporary = destination.with_name(f".{destination.stem}-normalizing{destination.suffix}")
        temporary.write_bytes(b"0" * 1_000_001)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(metadata, "_ffmpeg_output", encoded)
    assert metadata._normalize_video(
        source,
        destination,
        expected_duration=193.0,
    ) is True


def test_verified_official_video_may_be_much_longer_than_album_audio(
    monkeypatch, tmp_path
):
    source = tmp_path / "official-video.mp4"
    destination = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(metadata, "_video_info", Mock(return_value=(1920, 1080, 372.0)))
    monkeypatch.setattr(metadata, "_video_has_motion", Mock(return_value=True))
    monkeypatch.setattr(metadata, "_detected_crop", Mock(return_value="crop=1920:1080:0:0"))

    def encoded(*_args, **_kwargs):
        temporary = destination.with_name(f".{destination.stem}-normalizing{destination.suffix}")
        temporary.write_bytes(b"0" * 1_000_001)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(metadata, "_ffmpeg_output", encoded)

    assert metadata._normalize_video(
        source,
        destination,
        expected_duration=187.944,
        allow_extended_duration=True,
    ) is True


def test_unverified_long_video_is_still_rejected(monkeypatch, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0" * 1_000_001)
    monkeypatch.setattr(metadata, "_video_info", Mock(return_value=(1920, 1080, 372.0)))
    monkeypatch.setattr(metadata, "_video_has_motion", Mock(return_value=True))

    assert metadata.video_file_is_ready(
        clip,
        expected_duration=187.944,
        allow_extended_duration=False,
    ) is False
    assert metadata.video_file_is_ready(
        clip,
        expected_duration=187.944,
        allow_extended_duration=True,
    ) is True


def test_local_clip_is_ready_only_when_complete_playable_and_matches_song(monkeypatch, tmp_path):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"0" * 1_000_001)
    monkeypatch.setattr(metadata, "_video_info", Mock(return_value=(1280, 720, 206.0)))
    monkeypatch.setattr(metadata, "_video_has_motion", Mock(return_value=True))

    assert metadata.video_file_is_ready(clip, expected_duration=193.0) is True

    metadata._video_info.return_value = (1280, 720, 400.0)
    assert metadata.video_file_is_ready(clip, expected_duration=193.0) is False
    clip.write_bytes(b"partial")
    assert metadata.video_file_is_ready(clip, expected_duration=193.0) is False

def test_youtube_download_has_a_format_fallback_and_quiet_logger(monkeypatch, tmp_path):
    captured = {}

    class FakeYoutubeDL:
        def __init__(self, options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def download(self, _urls):
            return 1

    monkeypatch.setattr(metadata, "YoutubeDL", FakeYoutubeDL)

    assert metadata._download_youtube_video("DAaLa3vF8sU", tmp_path) is False
    assert captured["format"].endswith("bestvideo/best")
    assert isinstance(captured["logger"], metadata._YtDlpLogger)


def test_training_media_creates_cover_and_validated_local_clip(monkeypatch, tmp_path):
    patch_attrs(
        monkeypatch,
        metadata,
        _youtube_video_candidates=Mock(return_value=["DAaLa3vF8sU"]),
        _download_square_cover=Mock(return_value=True),
        _download_youtube_video=Mock(return_value=True),
        video_file_is_ready=Mock(return_value=False),
    )

    result = metadata.prepare_training_media(
        "Song",
        "Artist",
        tmp_path,
        cover_url="https://example.test/cover.jpg",
    )

    assert result == {
        "cover_status": "ready",
        "video_status": "ready",
        "video_id": "DAaLa3vF8sU",
        "warnings": [],
    }
    metadata._download_square_cover.assert_called_once_with(
        "https://example.test/cover.jpg", tmp_path
    )
    metadata._download_youtube_video.assert_called_once_with(
        "DAaLa3vF8sU",
        tmp_path,
        expected_duration=None,
    )


def test_training_media_retries_ranked_video_candidates_until_one_is_complete(
    monkeypatch, tmp_path
):
    """A broken/private first search result must not fail the whole song."""
    monkeypatch.setattr(
        metadata,
        "_youtube_video_candidates",
        Mock(return_value=["FIRSTCLIP01", "SECONDCLIP2"]),
        raising=False,
    )
    monkeypatch.setattr(metadata, "_youtube_video_id", Mock(return_value="FIRSTCLIP01"))
    monkeypatch.setattr(metadata, "_download_square_cover", Mock(return_value=True))
    monkeypatch.setattr(metadata, "video_file_is_ready", Mock(return_value=False))
    download = Mock(side_effect=[False, True])
    monkeypatch.setattr(metadata, "_download_youtube_video", download)

    result = metadata.prepare_training_media("Song", "Artist", tmp_path)

    assert result["video_status"] == "ready"
    assert result["video_id"] == "SECONDCLIP2"
    assert [call.args[0] for call in download.call_args_list] == [
        "FIRSTCLIP01",
        "SECONDCLIP2",
    ]


def test_training_media_caps_failed_clip_attempts_to_keep_processing_bounded(
    monkeypatch, tmp_path
):
    candidates = [f"CLIPID0000{index}" for index in range(6)]
    monkeypatch.setattr(
        metadata, "_youtube_video_candidates", Mock(return_value=candidates)
    )
    monkeypatch.setattr(metadata, "_download_square_cover", Mock(return_value=True))
    monkeypatch.setattr(metadata, "video_file_is_ready", Mock(return_value=False))
    download = Mock(return_value=False)
    monkeypatch.setattr(metadata, "_download_youtube_video", download)

    result = metadata.prepare_training_media("Song", "Artist", tmp_path)

    assert result["video_status"] == "fallback"
    assert download.call_count == 4


def test_training_media_starts_in_a_dedicated_spawn_process(monkeypatch, tmp_path):
    created = {}

    class FakeQueue:
        def get(self, timeout):
            assert timeout == 0.25
            return "ok", {"cover_status": "ready", "video_status": "ready"}

        def close(self):
            return None

        def cancel_join_thread(self):
            return None

    class FakeProcess:
        pid = 42
        exitcode = 0

        def __init__(self, **kwargs):
            created.update(kwargs)
            self.started = False

        def start(self):
            self.started = True

        def is_alive(self):
            return False

        def join(self, timeout):
            assert timeout in {1, 2}

    class FakeContext:
        def Queue(self, size):
            assert size == 1
            return FakeQueue()

        def Process(self, **kwargs):
            process = FakeProcess(**kwargs)
            created["process"] = process
            return process

    get_context = Mock(return_value=FakeContext())
    monkeypatch.setattr(metadata_media_process.multiprocessing, "get_context", get_context)

    process = metadata.start_training_media_process(
        "Song",
        "Artist",
        tmp_path,
        expected_duration=193.25,
    )

    get_context.assert_called_once_with("spawn")
    assert created["process"].started is True
    assert created["daemon"] is True
    assert created["name"] == "audio-v2-media"
    assert created["args"][4] == 193.25
    assert process.pid == 42
    assert process.result()["video_status"] == "ready"


def test_enrichment_persists_missing_metadata(monkeypatch):
    song = SimpleNamespace(
        id="song",
        title="Title",
        artist="Artist",
        genre=None,
        video_url=None,
    )
    database = Mock()
    def download(_video_id, output_dir, **_kwargs):
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / metadata.LOCAL_VIDEO_NAME).write_bytes(b"video")
        (output_dir / metadata.LOCAL_VIDEO_SOURCE_NAME).write_text("{}", encoding="utf-8")
        return True

    patch_attrs(
        monkeypatch,
        metadata,
        SessionLocal=Mock(return_value=database),
        _itunes_genre=Mock(return_value="Rock"),
        _youtube_video_id=Mock(return_value="DAaLa3vF8sU"),
        _download_youtube_video=Mock(side_effect=download),
        commit=Mock(),
    )
    monkeypatch.setattr(metadata.song_service, "resolve_output_dir", Mock(return_value=Path("output")))
    monkeypatch.setattr(metadata.repositories, "get_song", Mock(return_value=song))
    invalidate = Mock()
    monkeypatch.setattr(metadata.revision_cache, "invalidate", invalidate)

    metadata.enrich_song("song")

    assert song.genre == "Rock"
    assert song.video_url == metadata.LOCAL_VIDEO_URL
    # Two short-lived sessions now, not one held open across the download:
    # one commits the genre before the download starts, a second (fresh)
    # one commits the video_url after it finishes -- see enrich_song's
    # comment on why the download must not happen with a session open.
    assert metadata.commit.call_args_list == [call(database), call(database)]
    assert invalidate.call_args_list == [call(song), call(song)]
    assert database.close.call_count == 2


def test_run_enrichment_always_clears_the_admission_marker(monkeypatch):
    # enrich_song() has several early-return paths (no video found is the
    # common case without a configured video source) that never reach a
    # trailing "discard from _active" statement -- that responsibility now
    # lives in _run_enrichment's finally, which must run it regardless of
    # how enrich_song exits: normally, via an early return, or via an
    # exception escaping it.
    monkeypatch.setattr(metadata, "_active", {"no-video", "boom"})
    monkeypatch.setattr(metadata, "enrich_song", Mock(return_value=None))
    metadata._run_enrichment("no-video")
    assert "no-video" not in metadata._active

    monkeypatch.setattr(metadata, "enrich_song", Mock(side_effect=RuntimeError("boom")))
    raises(RuntimeError, lambda: metadata._run_enrichment("boom"), match="boom")
    assert "boom" not in metadata._active


def test_enrichment_downloads_outside_song_lock_then_publishes_atomically(monkeypatch, tmp_path):
    output = tmp_path / "library" / "song"
    cache = tmp_path / "cache"
    output.mkdir(parents=True)
    song = SimpleNamespace(id="song", title="Title", artist="Artist", genre="Rock", video_url=None)
    database = Mock()
    locked = False

    @contextmanager
    def content_lock(_song_id):
        nonlocal locked
        locked = True
        try:
            yield
        finally:
            locked = False

    def download(_video_id, staging, **_kwargs):
        assert locked is False
        assert output not in staging.parents and staging != output
        (staging / metadata.LOCAL_VIDEO_NAME).write_bytes(b"video")
        (staging / metadata.LOCAL_VIDEO_SOURCE_NAME).write_text("{}", encoding="utf-8")
        return True

    patch_attrs(
        monkeypatch,
        metadata,
        SessionLocal=Mock(return_value=database),
        _itunes_genre=Mock(return_value=None),
        _youtube_video_id=Mock(return_value="DAaLa3vF8sU"),
        _download_youtube_video=Mock(side_effect=download),
        commit=Mock(),
    )
    monkeypatch.setattr(metadata.config, "CACHE_DIR", cache)
    monkeypatch.setattr(metadata.repositories, "get_song", Mock(return_value=song))
    monkeypatch.setattr(metadata.song_service, "resolve_output_dir", Mock(return_value=output))
    monkeypatch.setattr(metadata.song_service, "song_content_lock", content_lock)
    monkeypatch.setattr(metadata.song_service, "library_write_lock", lambda: content_lock("library"))
    monkeypatch.setattr(metadata, "_active", {"song"})

    metadata.enrich_song("song")

    assert (output / metadata.LOCAL_VIDEO_NAME).read_bytes() == b"video"
    assert (output / metadata.LOCAL_VIDEO_SOURCE_NAME).is_file()
    assert song.video_url == metadata.LOCAL_VIDEO_URL
    assert not cache.exists() or not any(cache.rglob("clip.mp4"))


def test_enqueue_registers_supervised_task_and_rolls_back_when_admission_is_closed(monkeypatch):
    start = Mock(return_value=True)
    monkeypatch.setattr(metadata.background_task_supervisor, "start_task", start)
    monkeypatch.setattr(metadata, "_active", set())
    assert metadata.enqueue("song") is True
    start.assert_called_once_with("metadata-song", metadata._run_enrichment, ("song",))
    assert metadata.enqueue("song") is False

    monkeypatch.setattr(metadata, "_active", set())
    start.reset_mock(return_value=True)
    start.return_value = False
    assert metadata.enqueue("closed") is False
    assert "closed" not in metadata._active
