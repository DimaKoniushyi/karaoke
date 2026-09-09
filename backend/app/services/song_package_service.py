
from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import stat
import subprocess
import tempfile
import wave
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy.orm import Session

import config
import models
from AI.lyrics_document import flatten_word_notes, validate_lyrics_document
from AI.version import AI_BUILD_ID
from app.services import (
    revision_cache,
    song_artifacts,
    song_package_validation,
    song_service,
    storage_budget_service,
)
from app.services.db_utils import commit_refresh
from app.utils.atomic_files import atomic_write
from app.utils.hashing import sha256_file, sha256_stream
from app.utils.json_files import read_json, write_json
from app.utils.windows_paths import normalize_windows_component
from database import SessionLocal

MAX_PACKAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PACKAGE_FILES = 500
MAX_PACKAGE_COMPRESSION_RATIO = 200
MAX_MANIFEST_BYTES = 256 * 1024
PACKAGE_SCHEMA_VERSION = 6
REQUIRED_OUTPUT_KEYS = {"instrumental", "vocals", "lyricsSync"}
REQUIRED_OUTPUT_PATHS = {"instrumental.flac", "vocals.flac", "lyricsSync.json"}
REVISION_ARTIFACTS = ("lyricsSync.json",)
REVISION_RUNTIME_FIELDS = (
    "key_override", "tempo_override", "note_range_min", "note_range_max",
    "difficulty_override", "video_url", "show_lyrics", "show_notes", "optimized",
)
REVISION_ENTITY_FIELDS = ("title", "artist", "genre", "original_filename")
REVISION_SCHEMA_VERSION = 3
_SKIPPED_DIRS = {config.LOGS_DIRNAME, config.RECORDINGS_DIRNAME}
_IMPORT_JOURNAL_PREFIX = ".room-import-journal-"


def _processing_manifest(root: Path) -> dict[str, object]:
    return {"build": AI_BUILD_ID}


def _build_identity(payload: dict, error: str) -> str:
    build = payload.get("processing_build") or payload.get("build") or payload.get("version")
    if not isinstance(build, str) or not build.strip(): raise ValueError(error)
    return build.strip()



def _safe_output_relative(value: object, *, key: str) -> str:
    if not isinstance(value, str) or not value.strip(): raise ValueError(f"Song processing manifest has no {key} output")
    if any(token in value for token in ("\\", ":", "\x00")): raise ValueError(f"Song processing manifest has unsafe {key} output")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts): raise ValueError(f"Song processing manifest has unsafe {key} output")
    return path.as_posix()


def _artifact_relative(root: Path, key: str) -> str:
    path = song_artifacts.resolve_audio_artifact(root, key)
    if path is None: raise ValueError(f"Song processing manifest has no usable {key} output")
    if path.suffix.lower() not in config.ALLOWED_AUDIO_EXTENSIONS: raise ValueError(f"Song {key} format is not supported")
    return path.relative_to(root.resolve()).as_posix()


def _revision_artifact_paths(root: Path) -> tuple[str, ...]:
    return (*REVISION_ARTIFACTS, _artifact_relative(root, "instrumental"), _artifact_relative(root, "vocals"))


def _canonical_runtime_state(values: dict[str, object], mode: str) -> dict[str, object]:
    state = {field: values.get(field) for field in REVISION_RUNTIME_FIELDS}
    state["show_lyrics"] = True if state.get("show_lyrics") is None else bool(state["show_lyrics"])
    state["show_notes"] = True if state.get("show_notes") is None else bool(state["show_notes"])
    state["optimized"] = True if state.get("optimized") is None else bool(state["optimized"])
    if mode == "instrumental":
        state["show_lyrics"] = False
        state["show_notes"] = False
    elif mode == "lyrics": state["show_notes"] = False
    return state


def _runtime_revision_state(song: models.Song, *, mode: str | None = None) -> dict[str, object]:
    output = song_service.resolve_output_dir(song)
    resolved_mode, values = mode or _song_mode_from_files(output), {field: getattr(song, field, None) for field in REVISION_RUNTIME_FIELDS}
    return _canonical_runtime_state(values, resolved_mode)


def _canonical_entity_state(values: dict[str, object], *, source_name: str) -> dict[str, object]:
    title = str(values.get("title") or "").strip()
    return {
        "title": title,
        "artist": values.get("artist"),
        "genre": values.get("genre"),
        "original_filename": str(values.get("original_filename") or source_name),
    }


def _entity_revision_state(song: models.Song) -> dict[str, object]:
    source, values = song_service.resolve_source_path(song), {field: getattr(song, field, None) for field in REVISION_ENTITY_FIELDS}
    return _canonical_entity_state(values, source_name=source.name)


def _revision_payload(song: models.Song) -> dict[str, object]:
    root, source = song_service.resolve_output_dir(song), song_service.resolve_source_path(song)
    artifacts: dict[str, str] = {}
    for relative in _revision_artifact_paths(root):
        path = root / relative
        if not path.is_file(): raise ValueError(f"Song artifact is missing: {relative}")
        artifacts[relative] = sha256_file(path)
    if not source.is_file(): raise ValueError("Song source is missing")
    mode = _song_mode_from_files(root)
    return {
        "revision_schema": REVISION_SCHEMA_VERSION,
        "artifacts": artifacts,
        "source_sha256": sha256_file(source),
        "runtime": _runtime_revision_state(song, mode=mode),
        "entity": _entity_revision_state(song),
        "processing_build": _build_identity(_processing_manifest(root), "Song processing build identifier is invalid"),
    }


def _hash_revision_payload(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def compute_content_revision(song: models.Song) -> str: return _hash_revision_payload(_revision_payload(song))


def _revision_signature(song: models.Song) -> tuple[object, ...]:
    root, source = song_service.resolve_output_dir(song), song_service.resolve_source_path(song)
    paths = [root / relative for relative in _revision_artifact_paths(root)] + [source]
    stats: list[object] = [str(root)]
    for path in paths:
        if not path.is_file(): raise ValueError(f"Song artifact is missing: {path.name}")
        info = path.stat()
        label = str(path.relative_to(root)) if path == root or root in path.parents else "<source>"
        stats.extend((
            label, info.st_size, info.st_mtime_ns,
            getattr(info, "st_ctime_ns", None), getattr(info, "st_ino", None),
        ))
    stats.extend(_runtime_revision_state(song).items())
    stats.extend(_entity_revision_state(song).items())
    return tuple(stats)


def content_revision(song: models.Song) -> str:
    with song_service.song_content_lock(song.id), song_service.library_write_lock():
        signature = _revision_signature(song)
        if (cached := revision_cache.get(song.id, signature)) is not None: return cached
        revision = compute_content_revision(song)
        revision_cache.put(song.id, signature, revision)
        return revision



def _fresh_song_or_none(db: Session, song_id: str) -> models.Song | None:
    if (song := song_service.get_song(db, song_id)) is not None: db.refresh(song)
    return song


def _fresh_song(db: Session, song_id: str) -> models.Song:
    song = _fresh_song_or_none(db, song_id)
    if song is None: raise ValueError("Song not found")
    return song


@contextlib.contextmanager
def _locked_done_song(db: Session, song_id: str) -> Iterator[models.Song]:
    with song_service.song_content_lock(song_id), song_service.library_write_lock():
        song = _fresh_song(db, song_id)
        if not song_service.is_done(song): raise ValueError("Song processing is not complete")
        yield song


def content_revision_for_song(db: Session, song_id: str) -> str:
    with _locked_done_song(db, song_id) as song: return content_revision(song)


def content_revisions_for_songs(
    db: Session, song_ids: Iterable[str],
) -> list[tuple[str, str | None, str | None]]:
    """Fingerprint many songs under one library-lock hold instead of one per song.

    A caller (e.g. a room participant publishing their whole library) that
    fingerprints N songs one request at a time forces N separate acquisitions
    of the process-wide library_write_lock, serializing what should be a
    single batch of reads. Holding it once for the whole list keeps the same
    per-song locking/error semantics as content_revision_for_song while
    cutting that contention -- and the request count -- by N.
    """
    results: list[tuple[str, str | None, str | None]] = []
    with song_service.library_write_lock():
        for song_id in song_ids:
            try:
                results.append((song_id, content_revision_for_song(db, song_id), None))
            except (OSError, ValueError) as exc:
                results.append((song_id, None, str(exc)))
    return results


def find_song_id_by_content_revision(db: Session, revision: str) -> str | None:
    """Return the local id of a completed song with identical packaged content.

    Fingerprints one song at a time (unlike content_revisions_for_songs,
    which is for batch callers that need every song's revision) so a match
    early in the library doesn't pay for hashing the artifacts of every
    other song. Still holds the library lock for the whole search rather
    than once per song, for the same batching reason.
    """
    done_ids = [song.id for song in song_service.list_songs(db) if song_service.is_done(song)]
    with song_service.library_write_lock():
        for song_id in done_ids:
            try:
                if content_revision_for_song(db, song_id) == revision:
                    return song_id
            except (OSError, ValueError):
                continue
    return None


def build_package_for_song(
    db: Session, song_id: str, *, expected_revision: str | None = None,
) -> tuple[Path, str]:
    with _locked_done_song(db, song_id) as song: return build_package(song, expected_revision=expected_revision), song.slug

def invalidate_content_revision(song: models.Song) -> None: revision_cache.invalidate(song)

def _song_mode_from_files(output_dir: Path) -> str:
    try:
        lyrics_sync = json.loads((output_dir / "lyricsSync.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Song timeline artifacts are invalid") from exc
    if not isinstance(lyrics_sync, dict): raise ValueError("Song timeline artifacts are invalid")
    try:
        validate_lyrics_document(lyrics_sync)
    except ValueError as exc:
        raise ValueError("Song timeline artifacts are invalid") from exc
    words = lyrics_sync.get("words", [])
    if flatten_word_notes(lyrics_sync): return "melody"
    return "lyrics" if words else "instrumental"


def _manifest(song: models.Song) -> dict[str, object]:
    fields, output_dir = ('id', 'title', 'artist', 'genre', 'original_filename', 'slug', 'key_override', 'tempo_override', 'note_range_min', 'note_range_max', 'difficulty_override', 'video_url', 'show_lyrics', 'show_notes', 'optimized'), song_service.resolve_output_dir(song)
    mode = _song_mode_from_files(output_dir)
    runtime, values = _runtime_revision_state(song, mode=mode), {field: getattr(song, field) for field in fields}
    values.update(runtime)
    values.update(_entity_revision_state(song))
    return {
        "package_schema_version": PACKAGE_SCHEMA_VERSION,
        "processing_build": AI_BUILD_ID,
        "karaoke_mode": mode,
        "content_revision": content_revision(song),
        **values,
    }


def build_package(song: models.Song, *, expected_revision: str | None = None) -> Path:
    with song_service.song_content_lock(song.id), song_service.library_write_lock():
        source = song_service.resolve_source_path(song)
        output_dir = song_service.resolve_output_dir(song)
        if not source.is_file() or not output_dir.is_dir(): raise ValueError("Song files are incomplete")
        # Cached, not compute_content_revision() directly: the lock held for
        # this whole method means nothing can change the signature before
        # _manifest() below asks for the same revision again, so routing
        # through the cache here lets that second call hit it instead of
        # re-hashing every artifact a second time.
        current_revision = content_revision(song)
        if expected_revision is not None and current_revision != expected_revision: raise ValueError("Song revision changed before package export")

        package_payload = storage_budget_service.tree_size(output_dir)
        if source.resolve() != (output_dir / "instrumental.flac").resolve():
            package_payload += storage_budget_service.tree_size(source)
        with storage_budget_service.reserve(
            "song_package_export", config.CACHE_DIR, package_payload
        ):
            with tempfile.NamedTemporaryFile(
                prefix="karaoke-song-", suffix=".karaoke.zip", dir=config.CACHE_DIR, delete=False,
            ) as package:
                package_path = Path(package.name)
            try:
                manifest = _manifest(song)
                if manifest["content_revision"] != current_revision: raise ValueError("Song revision changed before package snapshot")
                with zipfile.ZipFile(package_path, "w", zipfile.ZIP_DEFLATED, compresslevel=4) as archive:
                    archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                    # After processing, source_path usually already points at
                    # output/instrumental.flac (see pipeline finalization) — don't
                    # store that file a second time under source/, it would just
                    # double the archive size for no benefit (import discards the
                    # source/ copy and aliases source_path to output/instrumental.flac
                    # anyway). Only write a distinct source/ entry when it's actually
                    # a different file.
                    if source.resolve() != (output_dir / "instrumental.flac").resolve():
                        archive.write(source, f"source/{source.name}")
                    for name in REQUIRED_OUTPUT_PATHS:
                        path = output_dir / name
                        if not path.is_file(): raise ValueError(f"Song artifact is missing: {name}")
                        archive.write(path, f"output/{name}")
                with zipfile.ZipFile(package_path) as archive:
                    members = _safe_members(archive)
                    exported_manifest = _read_manifest(archive)
                    _validate_semantic_package(archive, members, exported_manifest)
                    if exported_manifest.get("content_revision") != current_revision: raise ValueError("Exported package revision differs from requested revision")
                return package_path
            except Exception:
                package_path.unlink(missing_ok=True)
                raise

def _member_path(member: zipfile.ZipInfo) -> PurePosixPath:
    name = member.filename
    if not name or "\x00" in name or "\\" in name or ":" in name: raise ValueError("Song package contains an unsafe path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts): raise ValueError("Song package contains an unsafe path")
    return path


def _portable_destination_key(path: PurePosixPath) -> tuple[str, ...]:
    result: list[str] = []
    for raw_part in path.parts:
        try:
            part = normalize_windows_component(raw_part, reject_reserved=True)
        except ValueError as exc:
            raise ValueError("Song package contains a Windows-unsafe path") from exc
        result.append(part.casefold())
    return tuple(result)


def _final_destination(path: PurePosixPath, source_name: str | None = None) -> PurePosixPath | None:
    if path.parts[:1] == ("output",):
        if len(path.parts) == 1: return None
        return PurePosixPath(*path.parts[1:])
    return PurePosixPath(source_name) if path.parts[:1] == ('source',) and source_name is not None else None


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_PACKAGE_FILES: raise ValueError("Song package contains too many files")
    total_size = sum(member.file_size for member in members)
    if total_size > MAX_PACKAGE_BYTES: raise ValueError("Song package is too large")
    normalized: set[tuple[str, ...]] = set()
    for member in members:
        unix_mode = getattr(member, "external_attr", 0) >> 16
        if stat.S_IFMT(unix_mode) == stat.S_IFLNK: raise ValueError("Song package contains a symbolic link")
        if member.flag_bits & 0x1: raise ValueError("Encrypted song packages are not supported")
        if (
            not member.is_dir()
            and member.file_size > 1024 * 1024
            and member.file_size > max(1, member.compress_size) * MAX_PACKAGE_COMPRESSION_RATIO
        ):
            raise ValueError("Song package contains a suspiciously compressed file")
        path = _member_path(member)
        if path.parts[:1] == ("output",) and not song_artifacts.is_portable_output_path(
            PurePosixPath(*path.parts[1:])
        ):
            raise ValueError("Song package contains local-only/internal transaction metadata")
        key = _portable_destination_key(path)
        if key in normalized: raise ValueError("Song package contains duplicate/colliding paths")
        normalized.add(key)
    return members


def _read_manifest(archive: zipfile.ZipFile) -> dict[str, object]:
    try:
        info = archive.getinfo("manifest.json")
        if info.file_size > MAX_MANIFEST_BYTES: raise ValueError("Song package manifest is too large")
        with archive.open(info) as stream: payload = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES: raise ValueError("Song package manifest is too large")
        manifest = json.loads(payload)
    except (KeyError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("Song package manifest is invalid") from exc
    if not isinstance(manifest, dict): raise ValueError("Song package manifest is invalid")
    return manifest




def _archive_revision(archive: zipfile.ZipFile, manifest: dict[str, object]) -> str:
    artifacts: dict[str, str] = {}
    for relative in (*REVISION_ARTIFACTS, "instrumental.flac", "vocals.flac"):
        name = f"output/{relative}"
        try:
            info = archive.getinfo(name)
        except KeyError as exc:
            raise ValueError(f"Song package is missing revision artifact: {name}") from exc
        with archive.open(info) as stream: artifacts[relative] = sha256_stream(stream)
    mode = str(manifest.get("karaoke_mode") or "")
    runtime, source = _canonical_runtime_state({field: manifest.get(field) for field in REVISION_RUNTIME_FIELDS}, mode), _source_member(archive.infolist())
    with archive.open(source) as stream: source_sha256 = sha256_stream(stream)
    entity = _canonical_entity_state(
        {field: manifest.get(field) for field in REVISION_ENTITY_FIELDS},
        source_name=_member_path(source).name,
    )
    return _hash_revision_payload({
        "revision_schema": REVISION_SCHEMA_VERSION,
        "artifacts": artifacts,
        "source_sha256": source_sha256,
        "runtime": runtime,
        "entity": entity,
        "processing_build": _build_identity(manifest, "Song package processing build identifier is invalid"),
    })

def _number(value: object) -> float | None:
    if not isinstance(value, (int, float, str)): return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number == number else None


def _validate_wav_member(archive: zipfile.ZipFile, name: str) -> float:
    try:
        info = archive.getinfo(name)
        with archive.open(info) as stream, wave.open(stream, "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frames = audio.getnframes()
            if channels <= 0 or sample_width <= 0 or sample_rate <= 0 or frames <= 0: raise ValueError
            first_frame = audio.readframes(1)
            if len(first_frame) < channels * sample_width: raise ValueError
            return frames / sample_rate
    except (KeyError, EOFError, wave.Error, OSError, ValueError) as exc:
        raise ValueError(f"Song package contains invalid WAV audio: {name}") from exc


def _validate_archive_audio(
    archive: zipfile.ZipFile, member: zipfile.ZipInfo, *, label: str, min_duration: float = 0.02,
) -> None:
    suffix = Path(_member_path(member).name).suffix.lower()
    if suffix not in config.ALLOWED_AUDIO_EXTENSIONS: raise ValueError(f"Song package {label} format is not supported")
    if suffix == ".wav":
        if _validate_wav_member(archive, member.filename) <= min_duration: raise ValueError(f"Song package {label} audio has no usable duration")
        return
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f"karaoke-{label}-probe-", suffix=suffix, dir=config.CACHE_DIR, delete=False,
        ) as temp:
            temp_path = Path(temp.name)
            with archive.open(member) as source: shutil.copyfileobj(source, temp, length=1024 * 1024)
        probe = config.resolve_runtime_executable("ffprobe")
        result = subprocess.run(
            [probe, "-v", "error", "-select_streams", "a:0", "-show_entries",
             "stream=codec_type,duration:format=duration", "-of", "json", str(temp_path)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode != 0: raise ValueError(f"Song package {label} audio cannot be decoded")
        payload = json.loads(result.stdout or "{}")
        streams = payload.get("streams", []) if isinstance(payload, dict) else []
        durations = []
        if isinstance(payload, dict):
            fmt = payload.get("format")
            durations.append(_number(fmt.get("duration") if isinstance(fmt, dict) else None))
        if isinstance(streams, list):
            durations.extend(
                _number(item.get("duration"))
                for item in streams
                if isinstance(item, dict) and item.get("codec_type") == "audio"
            )
        has_audio = isinstance(streams, list) and any(
            isinstance(item, dict) and item.get("codec_type") == "audio" for item in streams
        )
        if not has_audio or not any(duration is not None and duration > min_duration for duration in durations): raise ValueError(f"Song package {label} audio has no usable duration")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ValueError(f"Song package {label} audio cannot be probed") from exc
    finally:
        if temp_path is not None: temp_path.unlink(missing_ok=True)



def _required_member(archive: zipfile.ZipFile, key: str) -> zipfile.ZipInfo:
    try:
        return archive.getinfo(f"output/{key}.flac")
    except KeyError as exc:
        raise ValueError(f"Song package manifest points to missing artifact: {key}") from exc


def _validate_semantic_package(archive: zipfile.ZipFile, members: list[zipfile.ZipInfo], manifest: dict[str, object]) -> None:
    if manifest.get("package_schema_version") != PACKAGE_SCHEMA_VERSION: raise ValueError("Unsupported or missing song package schema version")
    mode = manifest.get("karaoke_mode")
    if mode not in {"melody", "lyrics", "instrumental"}: raise ValueError("Song package has no supported karaoke mode")
    revision = manifest.get("content_revision")
    if not isinstance(revision, str) or not revision.startswith("sha256:") or len(revision) != 71: raise ValueError("Song package has no valid content revision")
    if revision != _archive_revision(archive, manifest): raise ValueError("Song package content revision does not match its artifacts")

    files = {member.filename for member in members if not member.is_dir()}
    if missing := {f"output/{name}" for name in REQUIRED_OUTPUT_PATHS} - files:
        raise ValueError(f"Song package is incomplete; missing: {', '.join(sorted(missing))}")
    _validate_archive_audio(archive, _required_member(archive, "instrumental"), label="instrumental")
    _validate_archive_audio(archive, _required_member(archive, "vocals"), label="vocals")
    source = _source_member(members)
    _validate_archive_audio(archive, source, label="source")
    song_package_validation.validate_timeline_artifacts(archive, mode)


def _package_identity(manifest: dict[str, object]) -> tuple[str, str]:
    song_id, title = str(manifest.get('id') or '').strip(), str(manifest.get('title') or '').strip()
    if not song_id or not title: raise ValueError("Song package has no song id or title")
    return song_id, title


def _source_member(members: list[zipfile.ZipInfo]) -> zipfile.ZipInfo:
    sources = [
        member
        for member in members
        if _member_path(member).parts[:1] == ("source",) and not member.is_dir()
    ]
    if len(sources) > 1: raise ValueError("Song package must contain one source file")
    if not sources:
        # No source/ entry means the exporter deduplicated it against
        # output/instrumental.flac (they were byte-identical) — that IS the
        # canonical source for this package.
        instrumental = next(
            (
                member
                for member in members
                if _member_path(member) == PurePosixPath("output/instrumental.flac") and not member.is_dir()
            ),
            None,
        )
        if instrumental is None: raise ValueError("Song package must contain one source file")
        return instrumental
    source = sources[0]
    if Path(_member_path(source).name).suffix.lower() not in config.ALLOWED_AUDIO_EXTENSIONS: raise ValueError("Song package source format is not supported")
    return source


def _copy_archive_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    destination: Path,
) -> None:
    def copy(target) -> None:
        with archive.open(member) as source: shutil.copyfileobj(source, target, length=1024 * 1024)

    atomic_write(destination, copy)


def _extract_output(
    archive: zipfile.ZipFile,
    members: list[zipfile.ZipInfo],
    destination: Path,
) -> None:
    for member in members:
        parts = _member_path(member).parts
        if member.is_dir() or not parts or parts[0] != "output": continue
        relative = PurePosixPath(*parts[1:])
        if not song_artifacts.is_portable_output_path(relative): raise ValueError("Song package contains local-only/internal transaction metadata")
        _copy_archive_member(archive, member, destination / Path(*relative.parts))


def _song_from_manifest(
    manifest: dict[str, object],
    *,
    song_id: str,
    title: str,
    slug: str,
    source_path: Path,
    output_dir: Path,
) -> models.Song:
    runtime, entity = _canonical_runtime_state({field: manifest.get(field) for field in REVISION_RUNTIME_FIELDS}, str(manifest.get('karaoke_mode') or '')), _canonical_entity_state({field: manifest.get(field) for field in REVISION_ENTITY_FIELDS}, source_name=source_path.name)
    return models.Song(
        id=song_id,
        title=entity["title"] or title,
        artist=entity["artist"],
        genre=entity["genre"],
        original_filename=entity["original_filename"],
        source_path=str(source_path),
        slug=slug,
        output_dir=str(output_dir),
        status=models.SongStatus.DONE,
        progress_step="imported",
        progress_percent=100.0,
        key_override=runtime["key_override"],
        tempo_override=runtime["tempo_override"],
        note_range_min=runtime["note_range_min"],
        note_range_max=runtime["note_range_max"],
        difficulty_override=runtime["difficulty_override"],
        video_url=runtime["video_url"],
        show_lyrics=runtime["show_lyrics"],
        show_notes=runtime["show_notes"],
        optimized=runtime["optimized"],
    )


def _apply_imported_song(target: models.Song, incoming: models.Song) -> None:
    for field in (
        "title", "artist", "genre", "original_filename", "source_path", "output_dir",
        "status", "progress_step", "progress_percent", "error_message", "key_override",
        "tempo_override", "note_range_min", "note_range_max", "difficulty_override",
        "video_url", "show_lyrics", "show_notes", "optimized",
    ):
        setattr(target, field, getattr(incoming, field))


def _same_revision(song: models.Song, revision: object) -> bool:
    if not isinstance(revision, str): return False
    try:
        return content_revision(song) == revision
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def _validate_destination_names(members: list[zipfile.ZipInfo], final_source_name: str) -> None:
    destinations: set[tuple[str, ...]] = set()
    for member in members:
        if member.is_dir(): continue
        path = _member_path(member)
        destination = _final_destination(path, final_source_name)
        # A distinct source/ entry (see _source_member) is no longer skipped
        # here: it now actually gets published as final_source_name too (see
        # _prepare_publish_tree), so if a crafted/coincidental source
        # filename collides with a required output artifact's own name, it
        # must be rejected here rather than silently overwriting it on import.
        if destination is None: continue
        key = _portable_destination_key(destination)
        if key in destinations: raise ValueError("Song package namespaces collide on the destination filesystem")
        destinations.add(key)


def _prepare_publish_tree(
    archive: zipfile.ZipFile, members: list[zipfile.ZipInfo], source_member: zipfile.ZipInfo,
    staging_root: Path, final_source_name: str,
) -> Path:
    staged_output, publish = staging_root / "output", staging_root / "publish"
    publish.mkdir()
    _extract_output(archive, members, staged_output)
    for item in staged_output.iterdir() if staged_output.exists() else ():
        target = publish / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    # A distinct source/ entry (not deduplicated against output/instrumental
    # .flac -- see _source_member) has to actually land on disk as
    # final_source_name. Without this, the imported song's source_path was
    # aliased to instrumental.flac regardless, while manifest.content_revision
    # was computed (in _archive_revision, at export time) from the real
    # source file's own hash -- the post-publish revision check a few lines
    # below in _publish_imported_package then always failed, so importing a
    # package with a genuinely distinct source file was simply impossible.
    if _member_path(source_member).parts[:1] == ("source",):
        _copy_archive_member(archive, source_member, publish / final_source_name)
    return publish


def _safe_library_root(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value: raise ValueError("Room import recovery journal is invalid")
    candidate = Path(value).expanduser().resolve()
    if candidate not in song_service.trusted_library_roots(): raise ValueError("Room import recovery journal references an untrusted library root")
    return candidate


def _safe_library_entry(name: object, *, root: Path | None = None) -> Path:
    if not isinstance(name, str) or not name or any(token in name for token in ("/", "\\", ":", "\x00")): raise ValueError("Room import recovery journal is invalid")
    trusted_root = (root or config.SONG_OUTPUT_DIR).resolve()
    if trusted_root not in song_service.trusted_library_roots(): raise ValueError("Room import recovery journal references an untrusted library root")
    candidate = (trusted_root / name).resolve()
    if candidate.parent != trusted_root: raise ValueError("Room import recovery journal escapes the library root")
    return candidate


def _preserve_local_output(backup_dir: Path | None, output_dir: Path) -> None:
    if backup_dir is None or not backup_dir.exists() or not output_dir.exists(): return
    for dirname in _SKIPPED_DIRS:
        old_local, new_local = backup_dir / dirname, output_dir / dirname
        if old_local.exists():
            if new_local.exists(): raise ValueError(f"Portable package collided with local-only directory: {dirname}")
            old_local.replace(new_local)


def _import_journal_path(output_dir: Path) -> Path:
    root = output_dir.resolve().parent
    if root not in song_service.trusted_library_roots(): raise ValueError("Song output directory is outside the trusted library roots")
    return root / f"{_IMPORT_JOURNAL_PREFIX}{uuid4().hex}.json"


def _write_import_journal(path: Path, payload: dict[str, object], phase: str) -> None:
    current = dict(payload)
    current["phase"] = phase
    write_json(path, current)


def _recover_import_journal(db: Session, journal_path: Path) -> None:
    journal: object = read_json(journal_path, default={})
    if not isinstance(journal, dict): raise ValueError("Room import recovery journal is invalid")
    phase = journal.get("phase")
    if phase not in {"prepared", "backup-created", "published", "db-committed"}: raise ValueError("Room import recovery journal is invalid")
    song_id, revision = journal.get("song_id"), journal.get("new_revision")
    if not isinstance(song_id, str) or not isinstance(revision, str): raise ValueError("Room import recovery journal is invalid")
    root_value = journal.get("library_root")
    root = _safe_library_root(root_value) if root_value is not None else _safe_library_root(str(journal_path.parent))
    if journal_path.resolve().parent != root: raise ValueError("Room import recovery journal is stored under a different library root")
    output_dir, backup_name = _safe_library_entry(journal.get('target'), root=root), journal.get('backup')
    backup_dir = _safe_library_entry(backup_name, root=root) if backup_name else None
    staging_name = journal.get("staging")
    staging_dir = _safe_library_entry(staging_name, root=root) if staging_name else None

    # A crash between staging_root being populated and the in-process
    # except/finally in _publish_imported_package removing it leaves that
    # full copy of the imported output tree with no reference anywhere else
    # -- clean it up here regardless of which branch below actually runs.
    try:
        if phase == "prepared":
            journal_path.unlink(missing_ok=True)
            return

        song, committed = _fresh_song_or_none(db, song_id), False
        if song is not None and output_dir.exists():
            try:
                committed = compute_content_revision(song) == revision
            except (OSError, ValueError, TypeError, AttributeError):
                committed = False

        if committed or phase == "db-committed":
            if not committed: raise ValueError("Room import recovery found a committed journal with mismatched DB state")
            _preserve_local_output(backup_dir, output_dir)
            if backup_dir is not None: shutil.rmtree(backup_dir, ignore_errors=True)
            journal_path.unlink(missing_ok=True)
            assert song is not None  # noqa: S101 -- committed can only be True when song was found above
            invalidate_content_revision(song)
            return

        if output_dir.exists(): shutil.rmtree(output_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists(): backup_dir.replace(output_dir)
        journal_path.unlink(missing_ok=True)
    finally:
        if staging_dir is not None:
            shutil.rmtree(staging_dir, ignore_errors=True)


def recover_import_transactions() -> None:
    roots = sorted(song_service.trusted_library_roots(), key=lambda path: str(path).casefold())
    journals = [
        journal
        for root in roots if root.exists()
        for journal in sorted(root.glob(f"{_IMPORT_JOURNAL_PREFIX}*.json"))
    ]
    if not journals: return
    db = SessionLocal()
    try:
        for journal_path in journals:
            try:
                _recover_import_journal(db, journal_path)
            except Exception:
                db.rollback()
                continue
    finally:
        db.close()


def _publish_imported_package(
    db: Session, archive: zipfile.ZipFile, members: list[zipfile.ZipInfo],
    manifest: dict[str, object], existing: models.Song | None, source_member: zipfile.ZipInfo,
    *, song_id: str, title: str, slug: str, final_source_name: str, output_dir: Path,
) -> models.Song:
    # The final publish is an atomic directory rename. Windows cannot perform
    # that rename across volumes (WinError 17), so the fully validated tree
    # must be staged beside its destination rather than in the global cache,
    # which may live on another drive.
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}.room-import-",
            dir=output_dir.parent,
        )
    )
    backup_dir = (
        output_dir.with_name(f".{output_dir.name}.room-backup-{uuid4().hex}")
        if existing
        else None
    )
    journal_path, published, db_committed = _import_journal_path(output_dir), False, False
    journal = {
        "operation": "room-import",
        "song_id": song_id,
        "library_root": str(output_dir.resolve().parent),
        "target": output_dir.name,
        "backup": backup_dir.name if backup_dir is not None else None,
        # Recorded so a hard crash (kill/power loss) mid-import -- before the
        # in-process except/finally below gets a chance to remove
        # staging_root itself -- still lets the next startup's
        # recover_import_transactions() find and delete it; without this the
        # staging directory (a full copy of the imported output tree) has no
        # reference anywhere and accumulates on disk forever.
        "staging": staging_root.name,
        "new_revision": manifest.get("content_revision"),
    }
    try:
        publish = _prepare_publish_tree(archive, members, source_member, staging_root, final_source_name)
        _write_import_journal(journal_path, journal, "prepared")
        if existing is not None:
            if backup_dir is None: raise RuntimeError("Room import backup path was not created")
            output_dir.replace(backup_dir)
            _write_import_journal(journal_path, journal, "backup-created")
        publish.replace(output_dir)
        published = True
        _write_import_journal(journal_path, journal, "published")

        import_manifest = dict(manifest)
        import_manifest.setdefault("original_filename", _member_path(source_member).name)
        incoming = _song_from_manifest(
            import_manifest, song_id=song_id, title=title, slug=slug,
            source_path=output_dir / final_source_name, output_dir=output_dir,
        )
        if compute_content_revision(incoming) != manifest.get("content_revision"): raise ValueError("Imported song would not preserve the package content revision")
        if existing is None:
            db.add(incoming)
            saved = commit_refresh(db, incoming)
        else:
            _apply_imported_song(existing, incoming)
            saved = commit_refresh(db, existing)
        db_committed = True
        with contextlib.suppress(OSError): _write_import_journal(journal_path, journal, "db-committed")

        _preserve_local_output(backup_dir, output_dir)
        if backup_dir is not None: shutil.rmtree(backup_dir, ignore_errors=True)
        shutil.rmtree(staging_root, ignore_errors=True)
        journal_path.unlink(missing_ok=True)
        invalidate_content_revision(saved)
        return saved
    except Exception:
        if db_committed:
            shutil.rmtree(staging_root, ignore_errors=True)
            raise
        db.rollback()
        if published and output_dir.exists(): shutil.rmtree(output_dir, ignore_errors=True)
        if backup_dir is not None and backup_dir.exists() and not output_dir.exists(): backup_dir.replace(output_dir)
        shutil.rmtree(staging_root, ignore_errors=True)
        journal_path.unlink(missing_ok=True)
        raise

def import_package(db: Session, package_path: Path, *, expected_revision: str | None = None) -> models.Song:
    with zipfile.ZipFile(package_path) as archive:
        members = _safe_members(archive)
        manifest = _read_manifest(archive)
        _validate_semantic_package(archive, members, manifest)
        song_id, title = _package_identity(manifest)
        revision = manifest["content_revision"]
        if expected_revision is not None and revision != expected_revision: raise ValueError("Imported package revision differs from the expected room revision")
        source_member = _source_member(members)
        # _source_member returns output/instrumental.flac itself when the
        # exporter deduplicated an identical source (see its docstring) --
        # only a genuine source/<name> entry gets published as its own file
        # (see _prepare_publish_tree); the aliasing to instrumental.flac
        # stays the same as before for the dedup case.
        final_source_name = (
            _member_path(source_member).name
            if _member_path(source_member).parts[:1] == ("source",)
            else "instrumental.flac"
        )
        _validate_destination_names(members, final_source_name)
        base_slug = song_service.slugify(str(manifest.get("slug") or title), "song")
        from app.services import pipeline_service, recording_service
        if pipeline_service.is_processing(song_id): raise ValueError("Cannot replace a song while it is being processed")
        if recording_service.has_active_recording(song_id): raise ValueError("Cannot replace a song while a recording session is active")
        with song_service.song_content_lock(song_id), song_service.library_write_lock():
            existing = _fresh_song_or_none(db, song_id)
            if existing is not None and _same_revision(existing, revision): return existing
            slug = existing.slug if existing is not None else song_service.make_unique_slug(db, base_slug)
            output_dir = song_service.resolve_output_dir(existing) if existing is not None else config.SONG_OUTPUT_DIR / slug
            package_bytes = sum(member.file_size for member in members if not member.is_dir())
            with storage_budget_service.reserve(
                "song_package_import", output_dir, package_bytes
            ):
                return _publish_imported_package(
                    db, archive, members, manifest, existing, source_member,
                    song_id=song_id, title=title, slug=slug,
                    final_source_name=final_source_name, output_dir=output_dir,
                )
