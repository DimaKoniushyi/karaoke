from __future__ import annotations

import json
import zipfile

from AI.lyrics_document import flatten_word_notes, validate_lyrics_document

MAX_LYRICS_BYTES = 16 * 1024 * 1024


def _archive_json(
    archive: zipfile.ZipFile, name: str, *, max_bytes: int
) -> object:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise ValueError(
            f"Song package is missing required artifact: {name}"
        ) from exc
    if info.file_size > max_bytes:
        raise ValueError(f"Song package artifact is too large: {name}")
    with archive.open(info) as stream:
        payload = stream.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ValueError(f"Song package artifact is too large: {name}")
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"Song package artifact is invalid JSON: {name}"
        ) from exc


def validate_timeline_artifacts(
    archive: zipfile.ZipFile, mode: object
) -> None:
    lyrics_sync = _archive_json(
        archive, "output/lyricsSync.json", max_bytes=MAX_LYRICS_BYTES
    )
    if not isinstance(lyrics_sync, dict):
        raise ValueError("Song package lyricsSync.json is structurally invalid")
    try:
        validate_lyrics_document(lyrics_sync)
    except ValueError as exc:
        raise ValueError(
            "Song package lyricsSync.json is structurally invalid"
        ) from exc
    words = lyrics_sync.get("words", [])
    notes = flatten_word_notes(lyrics_sync)
    if mode == "melody" and not notes:
        raise ValueError("Melody package has no vocal notes")
    if mode == "lyrics" and not words:
        raise ValueError("Lyrics package has no timed words")
