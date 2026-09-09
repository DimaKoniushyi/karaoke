from __future__ import annotations

import io
import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


@dataclass(frozen=True, slots=True)
class AudioMetadata:
    artist: str
    title: str
    genre: str | None = None
    cover_url: str | None = None
    video_url: str | None = None
    cover_urls: tuple[str, ...] = ()


MetadataProvider = Callable[[str, str], AudioMetadata | None]

_CYRILLIC_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d",
    "е": "e", "ё": "e", "є": "ye", "ж": "zh", "з": "z", "и": "i",
    "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})


def _identity(value: str) -> str:
    return " ".join(re.findall(r"[\w']+", value.casefold(), flags=re.UNICODE))


def _same_identity(expected: str, actual: str) -> bool:
    left, right = _identity(expected), _identity(actual)
    if not (left and right):
        return False
    if left == right or SequenceMatcher(None, left, right).ratio() >= 0.86:
        return True
    return SequenceMatcher(
        None, left.translate(_CYRILLIC_LATIN), right.translate(_CYRILLIC_LATIN)
    ).ratio() >= 0.58


def deezer_metadata(artist: str, title: str) -> AudioMetadata | None:
    query = f'artist:"{artist}" track:"{title}"'
    url = "https://api.deezer.com/search?" + urllib.parse.urlencode(
        {"q": query, "limit": 20}
    )
    try:
        payload = _request_json(url)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        row_artist = str((row.get("artist") or {}).get("name") or "")
        row_title = str(row.get("title_short") or row.get("title") or "")
        if not (_same_identity(artist, row_artist) and _same_identity(title, row_title)):
            continue
        album = row.get("album") if isinstance(row.get("album"), dict) else {}
        cover = str(album.get("cover_xl") or album.get("cover_big") or "") or None
        return AudioMetadata(artist=artist, title=title, cover_url=cover)
    return None


def _request_json(url: str, timeout: float = 5.0) -> object:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "A&D Voice/0.4"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read(2_000_000).decode("utf-8"))


def itunes_metadata(artist: str, title: str) -> AudioMetadata | None:
    queries = list(dict.fromkeys((
        f"{artist} {title}".strip(),
        f"{artist.translate(_CYRILLIC_LATIN)} {title.translate(_CYRILLIC_LATIN)}".strip(),
    )))
    for query in queries:
        url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
            {"term": query, "media": "music", "entity": "song", "limit": 50}
        )
        try:
            payload = _request_json(url)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        rows = payload.get("results", []) if isinstance(payload, dict) else []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            row_artist = str(row.get("artistName") or "")
            row_title = str(row.get("trackName") or "")
            if not (_same_identity(artist, row_artist) and _same_identity(title, row_title)):
                continue
            artwork = str(row.get("artworkUrl100") or "") or None
            if artwork:
                artwork = re.sub(r"/\d+x\d+bb\.", "/1200x1200bb.", artwork)
            return AudioMetadata(
                artist=artist,
                title=title,
                genre=str(row.get("primaryGenreName") or "").strip() or None,
                cover_url=artwork,
            )
    return None


def resolve_audio_metadata(
    *,
    artist: str,
    title: str,
    genre: str | None = None,
    cover_url: str | None = None,
    providers: Iterable[MetadataProvider] = (itunes_metadata, deezer_metadata),
) -> AudioMetadata:
    result = AudioMetadata(
        artist=artist.strip(),
        title=title.strip(),
        genre=genre.strip() if genre and genre.strip() else None,
        cover_url=cover_url.strip() if cover_url and cover_url.strip() else None,
    )
    cover_candidates = [result.cover_url] if result.cover_url else []
    for provider in providers:
        try:
            candidate = provider(result.artist, result.title)
        except Exception:
            continue
        if candidate is None or not (
            _same_identity(result.artist, candidate.artist)
            and _same_identity(result.title, candidate.title)
        ):
            continue
        for candidate_url in (candidate.cover_url, *candidate.cover_urls):
            if candidate_url and candidate_url not in cover_candidates:
                cover_candidates.append(candidate_url)
        result = replace(
            result,
            genre=result.genre or candidate.genre,
            cover_url=result.cover_url or candidate.cover_url,
            video_url=result.video_url or candidate.video_url,
        )
    return replace(result, cover_urls=tuple(cover_candidates))


def download_cover(url: str | None, destination: str | Path) -> bool:
    if not url:
        return False
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(
        url, headers={"Accept": "image/*", "User-Agent": "A&D Voice/0.4"}
    )
    try:
        with urllib.request.urlopen(request, timeout=8.0) as response:
            raw = response.read(8_000_000)
        with Image.open(io.BytesIO(raw)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            if min(image.size) < 160:
                return False
            side = min(1200, *image.size)
            image = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS)
            image.save(target, "JPEG", quality=91, optimize=True, progressive=True)
        return target.stat().st_size >= 8_000
    except (OSError, ValueError, UnidentifiedImageError):
        target.unlink(missing_ok=True)
        return False


def normalize_local_cover(source: str | Path, destination: str | Path) -> bool:
    target = Path(destination)
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
            if min(image.size) < 160:
                return False
            side = min(1200, *image.size)
            image = ImageOps.fit(image, (side, side), Image.Resampling.LANCZOS)
            image.save(target, "JPEG", quality=91, optimize=True, progressive=True)
        return target.stat().st_size >= 8_000
    except (OSError, ValueError, UnidentifiedImageError):
        target.unlink(missing_ok=True)
        return False
