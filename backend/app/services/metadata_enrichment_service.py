"""Fast, optional song metadata enrichment outside the AI processing path."""

from __future__ import annotations

import contextlib
import html
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import select

import config
import models
from app import repositories
from app.services import (
    background_task_supervisor,
    metadata_enrichment_runner,
    metadata_media_process,
    revision_cache,
    song_service,
)
from app.services.db_utils import commit
from database import SessionLocal

logger = logging.getLogger(__name__)
_worker_slots = threading.BoundedSemaphore(2)


class _YtDlpLogger:
    """Keep optional clip lookup failures out of the process-wide stderr log."""

    def debug(self, message: str) -> None:
        logger.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        logger.info("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        logger.info("yt-dlp warning: %s", message)

    def error(self, message: str) -> None:
        logger.info("yt-dlp error: %s", message)

try:
    from yt_dlp import YoutubeDL
except ImportError:  # Optional in lightweight developer environments.
    YoutubeDL = None

_YOUTUBE_ID_RE = re.compile(r'"videoId":"([\w-]{11})"')
_PLAIN_YOUTUBE_ID_RE = re.compile(r"[\w-]{11}")
_YOUTUBE_URL_ID_RE = re.compile(
    r"(?:youtu\.be/|youtube(?:-nocookie)?\.com/(?:watch\?(?:[^#]*&)?v=|embed/|shorts/|live/))([\w-]{11})",
    re.IGNORECASE,
)
_REJECT_VIDEO_HINTS = (
    "audio only", "official audio", "lyric", "lyrics", "karaoke", "visualizer",
    "static image", "still image", "slowed", "nightcore", "8d audio", "cover version",
    "reaction", "tutorial", "instrumental", "full album", "topic - auto-generated",
    "tribute", "текст песни", "слова песни", "караоке", "аудио", "обложка",
    "нейро", "нейросет", "ai generated", "ai-generated", "кавер", "трибьют",
)
_VIDEO_HINT_PENALTIES = (
    ("live", 18), ("concert", 18), ("концерт", 18), ("живое выступление", 18),
    ("fan clip", 28), ("fan-made", 28), ("fanmade", 28),
    ("фан-клип", 28), ("фан клип", 28),
)
_GOOD_VIDEO_HINT_SCORES = (
    ("official music video", 60),
    ("official video", 55),
    ("официальный клип", 55),
    ("премьера клипа", 40),
    ("music video", 35),
    ("анимационный клип", 30),
    ("клип", 20),
)
LOCAL_VIDEO_URL = "local:clip"
LOCAL_VIDEO_NAME = "clip.mp4"
LOCAL_VIDEO_SOURCE_NAME = "clip.source.json"
_MAX_VIDEO_DOWNLOAD_CANDIDATES = 4
_active: set[str] = set()
_validated_video_urls: set[str] = set()
_lock = threading.Lock()


TrainingMediaProcess = metadata_media_process.TrainingMediaProcess


def start_training_media_process(
    title: str,
    artist: str | None,
    output_dir: Path,
    *,
    expected_duration: float | None = None,
) -> TrainingMediaProcess:
    return metadata_media_process.start(
        title,
        artist,
        output_dir,
        expected_duration=expected_duration,
    )


def _request(url: str, timeout: float = 3.0) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/html;q=0.9",
            "User-Agent": "A&D Voice/0.3 metadata lookup",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(2_000_000)


def _itunes_genre(title: str, artist: str | None) -> str | None:
    query = " ".join(value for value in (artist, title) if value).strip()
    if not query: return None
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode({
        "term": query,
        "media": "music",
        "entity": "song",
        "limit": 3,
    })
    payload = json.loads(_request(url).decode("utf-8"))
    results = payload.get("results", []) if isinstance(payload, dict) else []
    for result in results if isinstance(results, list) else []:
        genre = result.get("primaryGenreName") if isinstance(result, dict) else None
        if isinstance(genre, str) and genre.strip(): return genre.strip()
    return None


def _words(value: str | None) -> set[str]:
    return set(re.findall(r"[\w\d]+", (value or "").casefold(), flags=re.UNICODE))


def _youtube_id_from_url(url: str | None) -> str | None:
    match = _YOUTUBE_URL_ID_RE.search(url or "")
    return match.group(1) if match else None


def _candidate_score(
    candidate_title: str,
    author: str | None,
    title: str,
    artist: str | None,
) -> int | None:
    normalized = " ".join(candidate_title.casefold().split())
    expected_normalized = " ".join(title.casefold().split())
    if any(
        hint in normalized and hint not in expected_normalized
        for hint in _REJECT_VIDEO_HINTS
    ):
        return None
    title_words = _words(title)
    candidate_words = _words(candidate_title)
    if title_words and not title_words & candidate_words:
        return None
    artist_words = _words(artist)
    author_words = _words(author)
    searchable_artist = candidate_words | author_words
    if artist_words and not artist_words & searchable_artist: return None
    score = 10 * len(title_words & candidate_words) + 6 * len(artist_words & searchable_artist)
    score += next(
        (weight for hint, weight in _GOOD_VIDEO_HINT_SCORES if hint in normalized),
        0,
    )
    score -= sum(weight for hint, weight in _VIDEO_HINT_PENALTIES if hint in normalized)
    return score


def _youtube_oembed(video_id: str) -> dict | None:
    url = "https://www.youtube.com/oembed?" + urllib.parse.urlencode({
        "url": f"https://www.youtube.com/watch?v={video_id}",
        "format": "json",
    })
    payload = json.loads(_request(url, timeout=2.5).decode("utf-8"))
    return payload if isinstance(payload, dict) and payload.get("title") else None


def _significant_frame_changes(frames: list, frame_size: int, threshold: float) -> int:
    """Count consecutive-frame pairs whose average per-pixel difference clears
    `threshold` -- used to reject still-image uploads cheaply (compression
    creates tiny changes even for a still image, so a real clip should
    contain several clearly different consecutive frames).
    """
    return sum(
        (sum(abs(left - right) for left, right in zip(previous, current, strict=True)) / frame_size) >= threshold
        for previous, current in zip(frames, frames[1:], strict=False)
    )


def _youtube_has_motion(video_id: str) -> bool | None:
    """Use YouTube's storyboard samples to reject still-image uploads cheaply."""
    try:
        raw = _request(f"https://i.ytimg.com/sb/{video_id}/storyboard3_L2/M0.jpg", timeout=2.5)
        with Image.open(io.BytesIO(raw)) as source:
            image = source.convert("L")
            if image.width < 50 or image.height < 50: return None
            tile_width = image.width // 5
            tile_height = image.height // 5
            frames = [
                image.crop((column * tile_width, row * tile_height,
                            (column + 1) * tile_width, (row + 1) * tile_height)).resize((32, 18)).getdata()
                for row in range(5)
                for column in range(5)
            ]
    except (OSError, UnidentifiedImageError, ValueError):
        return None
    return _significant_frame_changes(frames, 32 * 18, 4.0) >= 3


def _youtube_video_is_acceptable(video_id: str, title: str, artist: str | None) -> bool | None:
    try:
        metadata = _youtube_oembed(video_id)
    except Exception:
        return None
    acceptable = bool(metadata and _candidate_score(
        str(metadata.get("title", "")),
        str(metadata.get("author_name", "")),
        title,
        artist,
    ) is not None)
    return acceptable and _youtube_has_motion(video_id) is not False


def _youtube_video_candidates(title: str, artist: str | None) -> list[str]:
    """Return ranked, moving clip candidates instead of one fragile result."""
    query = " ".join(value for value in (artist, title, "official video") if value).strip()
    if not query: return []
    selected: list[str] = []
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if api_key:
        url = "https://www.googleapis.com/youtube/v3/search?" + urllib.parse.urlencode({
            "part": "snippet",
            "q": query,
            "type": "video",
            "videoEmbeddable": "true",
            "videoCategoryId": "10",
            "maxResults": 16,
            "key": api_key,
        })
        payload = json.loads(_request(url).decode("utf-8"))
        items = payload.get("items", []) if isinstance(payload, dict) else []
        ranked = []
        for item in items:
            video_id = item.get("id", {}).get("videoId") if isinstance(item, dict) else None
            snippet = item.get("snippet", {}) if isinstance(item, dict) else {}
            if not isinstance(video_id, str) or not _PLAIN_YOUTUBE_ID_RE.fullmatch(video_id): continue
            score = _candidate_score(
                str(snippet.get("title", "")),
                str(snippet.get("channelTitle", "")),
                title,
                artist,
            )
            if score is not None: ranked.append((score, video_id))
        for _, video_id in sorted(ranked, reverse=True):
            if _youtube_video_is_acceptable(video_id, title, artist) is True:
                selected.append(video_id)

    # Keyless fallback keeps local installations useful. It stores only the
    # public video id and streams through YouTube's muted embed player; no
    # copyrighted video file is copied into the song library.
    url = "https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
    page = html.unescape(_request(url).decode("utf-8", errors="ignore"))
    candidates = list(dict.fromkeys(_YOUTUBE_ID_RE.findall(page)))[:16]
    ranked = []
    for video_id in candidates:
        try:
            metadata = _youtube_oembed(video_id)
        except Exception:
            continue
        if not metadata: continue
        score = _candidate_score(
            str(metadata.get("title", "")),
            str(metadata.get("author_name", "")),
            title,
            artist,
        )
        if score is not None: ranked.append((score, video_id))
    for _, video_id in sorted(ranked, reverse=True):
        if video_id not in selected and _youtube_has_motion(video_id) is not False:
            selected.append(video_id)
    return selected


def _youtube_video_id(title: str, artist: str | None) -> str | None:
    candidates = _youtube_video_candidates(title, artist)
    return candidates[0] if candidates else None


def resolve_local_video(song: models.Song) -> Path | None:
    if song.video_url != LOCAL_VIDEO_URL:
        return None
    candidate = song_service.resolve_output_dir(song) / LOCAL_VIDEO_NAME
    return candidate if candidate.is_file() else None


def _ffmpeg_output(arguments: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    executable = str(config.FFMPEG_EXE)
    return subprocess.run(
        [executable, "-hide_banner", "-nostdin", *arguments],
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _video_info(path: Path) -> tuple[int, int, float] | None:
    try:
        # With no output target ffmpeg prints container metadata and exits
        # immediately; decoding the whole clip here would make enrichment
        # unnecessarily slow.
        result = _ffmpeg_output(["-i", str(path)], timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    details = result.stderr.decode("utf-8", errors="ignore")
    dimensions = re.search(r"Video:.*?\b(\d{3,5})x(\d{3,5})\b", details)
    duration = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", details)
    if not dimensions or not duration:
        return None
    hours, minutes, seconds = duration.groups()
    return (
        int(dimensions.group(1)),
        int(dimensions.group(2)),
        int(hours) * 3600 + int(minutes) * 60 + float(seconds),
    )


def _video_has_motion(path: Path, duration: float) -> bool:
    frame_size = 32 * 18
    sample_rate = max(0.05, min(1.0, 24 / max(1.0, duration)))
    try:
        result = _ffmpeg_output(
            [
                "-i", str(path), "-vf", f"fps={sample_rate:.6f},scale=32:18,format=gray",
                "-frames:v", "24", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
            ],
            timeout=45,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    frames = [
        result.stdout[offset:offset + frame_size]
        for offset in range(0, len(result.stdout) - frame_size + 1, frame_size)
    ]
    if len(frames) < 4:
        return False
    return _significant_frame_changes(frames, frame_size, 3.5) >= 3


def _video_duration_matches(
    duration: float,
    expected_duration: float | None,
    *,
    allow_extended_duration: bool,
) -> bool:
    if not expected_duration or expected_duration <= 0:
        return True
    maximum_drift = max(45.0, expected_duration * 0.25)
    if abs(duration - expected_duration) <= maximum_drift:
        return True
    # A verified official video can contain a substantial narrative intro,
    # interlude or outro. Only relax the upper bound; suspiciously short
    # results still fail the ordinary recording-length check.
    return bool(
        allow_extended_duration
        and duration > expected_duration
        and duration <= expected_duration * 3.0
    )


def video_file_is_ready(
    path: Path,
    *,
    expected_duration: float | None = None,
    allow_extended_duration: bool = False,
) -> bool:
    """Return true only for a complete, playable and song-length local clip."""
    try:
        if not path.is_file() or path.stat().st_size < 1_000_000:
            return False
    except OSError:
        return False
    info = _video_info(path)
    if not info:
        return False
    width, height, duration = info
    if width < 640 or height < 360 or duration < 20:
        return False
    if not _video_duration_matches(
        duration,
        expected_duration,
        allow_extended_duration=allow_extended_duration,
    ):
        return False
    return _video_has_motion(path, duration)


def _detected_crop(path: Path, width: int, height: int, duration: float) -> str:
    seek = max(0.0, min(duration * 0.35, max(0.0, duration - 8)))
    try:
        result = _ffmpeg_output(
            [
                "-ss", f"{seek:.3f}", "-i", str(path), "-t", "8",
                "-vf", "cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-",
            ],
            timeout=35,
        )
    except (OSError, subprocess.SubprocessError):
        return f"crop={width}:{height}:0:0"
    crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", result.stderr.decode("utf-8", errors="ignore"))
    if not crops:
        return f"crop={width}:{height}:0:0"
    crop_width, crop_height, x, y = Counter(crops).most_common(1)[0][0]
    if int(crop_width) < width * 0.55 or int(crop_height) < height * 0.55:
        return f"crop={width}:{height}:0:0"
    return f"crop={crop_width}:{crop_height}:{x}:{y}"


def _normalize_video(
    source: Path,
    destination: Path,
    *,
    expected_duration: float | None = None,
    allow_extended_duration: bool = False,
) -> bool:
    info = _video_info(source)
    if not info:
        return False
    width, height, duration = info
    if width < 640 or height < 360 or duration < 20 or not _video_has_motion(source, duration):
        return False
    if not _video_duration_matches(
        duration,
        expected_duration,
        allow_extended_duration=allow_extended_duration,
    ):
        return False
    crop = _detected_crop(source, width, height, duration)
    target_width, target_height = (1920, 1080) if height >= 1080 else (1280, 720)
    temporary = destination.with_name(f".{destination.stem}-normalizing{destination.suffix}")
    temporary.unlink(missing_ok=True)
    filter_chain = (
        f"{crop},scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
        f"crop={target_width}:{target_height}"
    )
    try:
        result = _ffmpeg_output(
            [
                "-y", "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn",
                "-vf", filter_chain, "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary),
            ],
            timeout=max(180, min(1800, int(duration * 4))),
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size < 1_000_000:
            return False
        os.replace(temporary, destination)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _download_youtube_video(
    video_id: str,
    output_dir: Path,
    *,
    expected_duration: float | None = None,
) -> bool:
    if YoutubeDL is None:
        logger.info("Music video download skipped: yt-dlp is not installed")
        return False
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / LOCAL_VIDEO_NAME
    source_metadata = output_dir / LOCAL_VIDEO_SOURCE_NAME
    for stale in output_dir.glob(".clip-download.*"):
        stale.unlink(missing_ok=True)
    options = {
        "format": (
            "bestvideo[vcodec^=avc1][ext=mp4][height>=1080]/"
            "best[vcodec^=avc1][ext=mp4][height>=1080]/"
            "bestvideo[vcodec^=avc1][ext=mp4][height>=720]/"
            "best[vcodec^=avc1][ext=mp4][height>=720]/"
            "bestvideo[ext=mp4][height>=720]/best[ext=mp4][height>=720]/"
            "bestvideo[height>=360]/best[height>=360]/"
            "bestvideo/best"
        ),
        "outtmpl": str(output_dir / ".clip-download.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "overwrites": True,
        "continuedl": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 2,
        "max_filesize": 400 * 1024 * 1024,
        "logger": _YtDlpLogger(),
    }
    try:
        with YoutubeDL(options) as downloader:
            result = downloader.download([f"https://www.youtube.com/watch?v={video_id}"])
        candidates = [
            path for path in output_dir.glob(".clip-download.*")
            if path.is_file() and path.suffix not in {".part", ".ytdl"}
        ]
        source = max(candidates, key=lambda path: path.stat().st_size) if candidates else None
        if result != 0 or source is None or not _normalize_video(
            source,
            destination,
            expected_duration=expected_duration,
            allow_extended_duration=True,
        ):
            destination.unlink(missing_ok=True)
            source_metadata.unlink(missing_ok=True)
            return False
        source_metadata.write_text(
            json.dumps({"video_id": video_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return True
    except Exception as exc:  # Network extraction is optional and must never break processing.
        logger.info("Music video download failed for %s: %s", video_id, exc)
        destination.unlink(missing_ok=True)
        source_metadata.unlink(missing_ok=True)
        return False
    finally:
        for stale in output_dir.glob(".clip-download.*"):
            stale.unlink(missing_ok=True)


def _download_square_cover(url: str, output_dir: Path) -> bool:
    """Save a consistent local cover from a verified song/video thumbnail."""
    if not url:
        return False
    destination = output_dir / "cover.jpg"
    temporary = output_dir / ".cover-preparing.jpg"
    temporary.unlink(missing_ok=True)
    try:
        raw = _request(url, timeout=8)
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            if image.width < 160 or image.height < 90:
                return False
            side = min(1200, image.width, image.height)
            image = ImageOps.fit(
                image,
                (side, side),
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            image.save(temporary, "JPEG", quality=90, optimize=True, progressive=True)
        if temporary.stat().st_size < 8_000:
            return False
        os.replace(temporary, destination)
        return True
    except (OSError, UnidentifiedImageError, ValueError):
        return False
    finally:
        temporary.unlink(missing_ok=True)


def prepare_training_media(
    title: str,
    artist: str | None,
    output_dir: Path,
    *,
    cover_url: str | None = None,
    expected_duration: float | None = None,
) -> dict[str, object]:
    """Create the same local visual assets used by a processed karaoke song."""
    output_dir.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []
    video_id: str | None = None
    video_candidates: list[str] = []
    try:
        video_candidates = _youtube_video_candidates(title, artist)
        video_id = video_candidates[0] if video_candidates else None
    except Exception as exc:
        warnings.append(f"Не удалось найти клип: {exc}")

    cover_ready = any(
        (output_dir / f"cover{suffix}").is_file()
        for suffix in (".jpg", ".png", ".webp")
    )
    if not cover_ready:
        thumbnail = cover_url or (
            f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg" if video_id else ""
        )
        cover_ready = _download_square_cover(thumbnail, output_dir)
        if not cover_ready and video_id:
            cover_ready = _download_square_cover(
                f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                output_dir,
            )
        if not cover_ready:
            warnings.append("Не удалось получить обложку песни")

    video_ready = video_file_is_ready(
        output_dir / LOCAL_VIDEO_NAME,
        expected_duration=expected_duration,
    )
    if not video_ready:
        for candidate_id in video_candidates[:_MAX_VIDEO_DOWNLOAD_CANDIDATES]:
            if _download_youtube_video(
                candidate_id,
                output_dir,
                expected_duration=expected_duration,
            ):
                video_ready = True
                video_id = candidate_id
                break
    if not video_ready:
        warnings.append(
            "Подходящий движущийся клип не найден; караоке использует стандартное видео"
        )
    return {
        "cover_status": "ready" if cover_ready else "fallback",
        "video_status": "ready" if video_ready else "fallback",
        "video_id": video_id,
        "warnings": warnings,
    }


def enrich_song(song_id: str) -> None:
    metadata_enrichment_runner.enrich(sys.modules[__name__], song_id)


def enqueue(song_id: str) -> bool:
    with _lock:
        if song_id in _active: return False
        _active.add(song_id)
    started = background_task_supervisor.start_task(
        f"metadata-{song_id}", _run_enrichment, (song_id,)
    )
    if not started:
        with _lock: _active.discard(song_id)
    return started


def _run_enrichment(song_id: str) -> None:
    # enrich_song() has several early returns (song deleted, no video needed,
    # song deleted again mid-download) that never reached its old end-of
    # -function `_active.discard(song_id)` -- any of those, or an unexpected
    # exception, used to leave song_id in _active forever, silently making
    # enqueue() refuse to ever retry that song again for the rest of the
    # process's life. Removing the admission marker here instead guarantees
    # it always runs, regardless of how enrich_song exits.
    with _worker_slots:
        try:
            enrich_song(song_id)
        finally:
            with _lock: _active.discard(song_id)


def enqueue_missing(limit: int = 8) -> int:
    db = SessionLocal()
    try:
        songs = list(db.scalars(
            select(models.Song)
            .where(models.Song.status == models.SongStatus.DONE)
            .order_by(models.Song.updated_at.desc())
            .limit(max(0, limit * 4))
        ))
    finally:
        db.close()
    song_ids = [
        song.id for song in songs
        if (
            not song.genre
            or not song.video_url
            or (song.video_url == LOCAL_VIDEO_URL and resolve_local_video(song) is None)
            or (
                _youtube_id_from_url(song.video_url)
            )
        )
    ][:max(0, limit)]
    return sum(enqueue(song_id) for song_id in song_ids)
