"""Prepare the common karaoke-training dataset from KaraFun KFN containers."""

from __future__ import annotations

import configparser
import hashlib
import io
import re
import shutil
import struct
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import config
from app.services import kar_dataset_service, song_service
from app.services.kar_alignment import retime_words
from app.utils.json_files import write_json

AES: Any
try:
    from Cryptodome.Cipher import AES as _AES
    AES = _AES
except ImportError:  # pragma: no cover - reported only by lightweight broken installations
    AES = None


MAX_KFN_BYTES = 256 * 1024 * 1024
_AUDIO_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
_MIDI_SUFFIXES = {".kar", ".mid", ".midi"}


class KfnSkipped(ValueError):
    """A valid KFN that cannot provide ready symbolic vocal notes."""


@dataclass(slots=True)
class KfnEntry:
    name: str
    kind: int
    payload: bytes


@dataclass(slots=True)
class KfnContainer:
    variant: str
    header: dict[str, Any]
    entries: list[KfnEntry]


def _u32(data: bytes, offset: int) -> tuple[int, int]:
    if offset + 4 > len(data):
        raise ValueError("Повреждённый KFN: неожиданный конец контейнера")
    return struct.unpack_from("<I", data, offset)[0], offset + 4


def _decode(value: bytes) -> str:
    value = value.rstrip(b"\x00")
    for encoding in ("utf-8-sig", "cp1251", "cp1250", "latin1"):
        try:
            return value.decode(encoding)
        except UnicodeDecodeError:
            continue
    return value.decode("utf-8", errors="replace")


def _parse_kfnb(data: bytes) -> KfnContainer:
    offset = 4
    header: dict[str, Any] = {}
    while True:
        if offset + 5 > len(data):
            raise ValueError("Повреждённый KFN: заголовок не завершён")
        tag = data[offset : offset + 4].decode("ascii", errors="replace")
        flag = data[offset + 4]
        offset += 5
        value: int | bytes
        if flag == 1:
            value, offset = _u32(data, offset)
        elif flag == 2:
            length, offset = _u32(data, offset)
            if length > len(data) - offset:
                raise ValueError("Повреждённый KFN: неверная длина поля заголовка")
            value = data[offset : offset + length]
            offset += length
        else:
            raise ValueError(f"Повреждённый KFN: неизвестный тип поля {flag}")
        header[tag] = value
        if tag == "ENDH":
            break

    count, offset = _u32(data, offset)
    if count > 10_000:
        raise ValueError("Повреждённый KFN: слишком много вложенных файлов")
    directory = []
    for _index in range(count):
        name_length, offset = _u32(data, offset)
        if name_length > len(data) - offset:
            raise ValueError("Повреждённый KFN: неверное имя вложенного файла")
        name = _decode(data[offset : offset + name_length])
        offset += name_length
        kind, offset = _u32(data, offset)
        length_out, offset = _u32(data, offset)
        relative_offset, offset = _u32(data, offset)
        length_in, offset = _u32(data, offset)
        encrypted, offset = _u32(data, offset)
        directory.append((name, kind, length_out, relative_offset, length_in, encrypted))

    payload_start = offset
    key = header.get("FLID", b"")
    entries = []
    for name, kind, length_out, relative_offset, length_in, encrypted in directory:
        start, end = payload_start + relative_offset, payload_start + relative_offset + length_in
        if start < payload_start or end > len(data):
            raise ValueError(f"Повреждённый KFN: данные {name!r} выходят за границы файла")
        payload = data[start:end]
        if encrypted & 1:
            if AES is None:
                raise RuntimeError("Для зашифрованного KFN требуется пакет pycryptodomex")
            if not isinstance(key, bytes) or len(key) != 16 or len(payload) % 16:
                raise ValueError("Повреждённый KFN: некорректный ключ или AES-блок")
            payload = AES.new(key, AES.MODE_ECB).decrypt(payload)
        if length_out > len(payload):
            raise ValueError(f"Повреждённый KFN: неверный размер {name!r}")
        entries.append(KfnEntry(name=name, kind=kind, payload=payload[:length_out]))
    return KfnContainer(variant="KFNB", header=header, entries=entries)


def _parse_zip(data: bytes) -> KfnContainer:
    entries = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        total = sum(item.file_size for item in archive.infolist())
        if total > MAX_KFN_BYTES * 2:
            raise ValueError("KFN содержит слишком много распакованных данных")
        for item in archive.infolist():
            if item.is_dir():
                continue
            name = Path(item.filename).name
            suffix = Path(name).suffix.casefold()
            kind = 1 if name.casefold() == "song.ini" else 2 if suffix in _AUDIO_SUFFIXES else 0
            entries.append(KfnEntry(name=name, kind=kind, payload=archive.read(item)))
    return KfnContainer(variant="ZIP", header={}, entries=entries)


def parse_kfn_container(path: str | Path) -> KfnContainer:
    source = Path(path)
    if source.suffix.casefold() != ".kfn":
        raise ValueError("Поддерживаются файлы .kar и .kfn")
    if source.stat().st_size > MAX_KFN_BYTES:
        raise ValueError("Файл .kfn превышает допустимый размер 256 МБ")
    data = source.read_bytes()
    if data.startswith(b"KFNB"):
        return _parse_kfnb(data)
    if data.startswith(b"PK"):
        return _parse_zip(data)
    if data.startswith(b"KFN3"):
        raise KfnSkipped("KFN3 пока пропущен: контейнер не предоставляет готовые MIDI-ноты")
    raise ValueError("Файл не является корректным KFN")


class _CaseSensitiveConfigParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _parse_ini(payload: bytes) -> configparser.ConfigParser:
    parser = _CaseSensitiveConfigParser(interpolation=None, strict=False)
    parser.read_string(_decode(payload))
    return parser


def _indexed_values(section: configparser.SectionProxy, prefix: str) -> list[str]:
    indexes = []
    for key in section:
        match = re.fullmatch(rf"{re.escape(prefix)}(\d+)", key, re.I)
        if match:
            indexes.append((int(match.group(1)), section[key]))
    return [value for _index, value in sorted(indexes)]


def _text_fragments(text: str) -> list[tuple[str, bool]]:
    fragments: list[tuple[str, bool]] = []
    words = re.findall(r"\S+", text)
    for word in words:
        if word == "_":
            fragments.append(("", True))
            continue
        syllables = word.strip("/").split("/")
        visible = [part.replace("_", " ") for part in syllables if part]
        for index, syllable in enumerate(visible):
            fragments.append((syllable, index == len(visible) - 1))
    return fragments


def _songini_words(parser: configparser.ConfigParser) -> list[dict[str, Any]]:
    candidates = []
    for name in parser.sections():
        section = parser[name]
        if section.get("ID", "") not in {"1", "2"}:
            continue
        marks: list[float] = []
        for raw in _indexed_values(section, "Sync"):
            marks.extend(float(value.strip()) / 100 for value in raw.split(",") if value.strip())
        fragments = []
        for raw in _indexed_values(section, "Text"):
            fragments.extend(_text_fragments(raw))
        paired = list(zip(marks, fragments, strict=False))
        if paired:
            candidates.append((len(paired), paired))
    if not candidates:
        raise ValueError("В KFN не найден синхронизированный текст Song.ini")
    paired = max(candidates, key=lambda item: item[0])[1]
    words, text, start = [], "", 0.0
    for index, (time, (fragment, word_end)) in enumerate(paired):
        if not text:
            start = time
        text += fragment
        if not word_end:
            continue
        next_time = paired[index + 1][0] if index + 1 < len(paired) else time + 0.5
        if text.strip():
            words.append(
                {
                    "text": text.strip(),
                    "start": round(start, 3),
                    "end": round(max(start + 0.04, next_time), 3),
                    "notes": [],
                }
            )
        text = ""
    if not words:
        raise ValueError("В KFN не удалось выделить синхронизированные слова")
    return words


def _midi_document(payload: bytes, words: list[dict[str, Any]], directory: Path):
    midi_path = directory / "embedded.kar"
    midi_path.write_bytes(payload)
    midi = kar_dataset_service._load_midi(midi_path)
    tracks = kar_dataset_service._track_events(midi)
    convert, initial_tempo = kar_dataset_service._tempo_converter(tracks, midi.ticks_per_beat)
    candidates = kar_dataset_service._notes_by_track(tracks, convert)
    labelled = [
        candidate
        for candidate in candidates
        if re.search(
            r"vocal|voice|melody|lead|sing|вокал|мелод",
            candidate[1],
            re.I,
        )
    ]
    if labelled:
        candidates = labelled
    elif len(candidates) != 1:
        raise KfnSkipped(
            "KFN пропущен: не удалось однозначно определить готовую вокальную MIDI-дорожку"
        )
    melody_track, notes = kar_dataset_service._select_melody_track(
        candidates, -1, words[0]["start"], words[-1]["end"]
    )
    if not notes or melody_track is None:
        raise KfnSkipped("KFN пропущен: внутри нет готовой вокальной MIDI-разметки нот")
    kar_dataset_service._attach_notes(words, notes)
    note_count = sum(len(word["notes"]) for word in words)
    if not note_count:
        raise KfnSkipped("KFN пропущен: MIDI-ноты не совпадают по времени с текстом")
    _title, _artist, key = kar_dataset_service._metadata(tracks)
    duration = max(
        words[-1]["end"],
        max((note["end"] for note in notes), default=0.0),
    )
    return round(60_000_000 / initial_tempo, 3), key, round(duration, 3), melody_track


def _header_text(container: KfnContainer, tag: str) -> str:
    value = container.header.get(tag, b"")
    return _decode(value) if isinstance(value, bytes) else ""


def _container_identity(
    container: KfnContainer,
    parser: configparser.ConfigParser,
    original_filename: str,
) -> tuple[str, str]:
    general = parser["General"] if parser.has_section("General") else {}
    filename_artist, filename_title = song_service.parse_filename_identity(original_filename)
    title = kar_dataset_service._clean_text(
        general.get("Title", "") or _header_text(container, "TITL") or filename_title
    )
    artist = kar_dataset_service._clean_text(
        general.get("Artist", "") or _header_text(container, "ARTS") or filename_artist or ""
    )
    title, artist = kar_dataset_service.normalize_karaoke_identity(title, artist)
    return artist, title


def inspect_kfn_identity(
    path: str | Path,
    *,
    original_filename: str | None = None,
) -> tuple[str, str]:
    container = parse_kfn_container(Path(path))
    songini_entry = next(
        (entry for entry in container.entries if entry.kind == 1 or entry.name.casefold() == "song.ini"),
        None,
    )
    if songini_entry is None:
        raise ValueError("В KFN отсутствует Song.ini")
    return _container_identity(
        container,
        _parse_ini(songini_entry.payload),
        original_filename or Path(path).name,
    )


def _write_embedded_audio(entry: KfnEntry, target: Path) -> bool:
    suffix = Path(entry.name).suffix.casefold()
    if suffix not in _AUDIO_SUFFIXES:
        suffix = ".bin"
    source = target / f".kfn-audio{suffix}"
    source.write_bytes(entry.payload)
    result = subprocess.run(
        [
            str(config.FFMPEG_EXE),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "flac",
            str(target / "original.flac"),
        ],
        capture_output=True,
        check=False,
        timeout=20 * 60,
    )
    source.unlink(missing_ok=True)
    ready = result.returncode == 0 and (target / "original.flac").is_file()
    if not ready:
        (target / "original.flac").unlink(missing_ok=True)
    return ready


def _refine_kfn_vocals(document, target: Path):
    match = kar_dataset_service._midi_audio_match(
        document,
        target / "vocals.flac",
        max_offset_seconds=kar_dataset_service._MAX_VOCAL_AUDIO_OFFSET_SECONDS,
    )
    if match["score"] < kar_dataset_service._MIN_MELODY_MATCH_SCORE:
        return None
    offset = round(
        float(match["offset_seconds"]) - kar_dataset_service._VOCAL_DISPLAY_LEAD_SECONDS, 3
    )
    scale = float(match["time_scale"])
    match = {**match, "offset_seconds": offset, "display_lead_seconds": 0.1}
    document = replace(
        document,
        bpm=float(match["audio_bpm"]),
        duration=max(0, document.duration * scale + offset),
        words=retime_words(document.words, scale, offset),
    )
    return document, kar_dataset_service._lyrics_payload(document, "kfn"), {
        **match,
        "status": "vocal-stem-refined",
    }


def _embedded_audio_entry(container):
    return next(
        (
            entry
            for entry in container.entries
            if entry.kind == 2 and not entry.payload.startswith(b"MThd")
        ),
        None,
    )


def prepare_kfn_file(
    path: str | Path,
    *,
    original_filename: str | None = None,
    title_override: str | None = None,
    artist_override: str | None = None,
    reuse_existing_audio: bool = False,
    output_root: str | Path = kar_dataset_service.DATASET_DIR,
    target_dir: str | Path | None = None,
    progress: kar_dataset_service.DatasetProgress | None = None,
    cancelled: kar_dataset_service.CancelCallback | None = None,
) -> dict[str, Any]:
    source = Path(path)
    kar_dataset_service._notify_dataset(
        progress, cancelled, "karaoke_parse", 2, "Читаем контейнер KFN"
    )
    container = parse_kfn_container(source)
    songini_entry = next(
        (entry for entry in container.entries if entry.kind == 1 or entry.name.casefold() == "song.ini"),
        None,
    )
    if songini_entry is None:
        raise ValueError("В KFN отсутствует Song.ini")
    parser = _parse_ini(songini_entry.payload)
    words = _songini_words(parser)
    midi_entry = next(
        (
            entry
            for entry in container.entries
            if entry.payload.startswith(b"MThd") or Path(entry.name).suffix.casefold() in _MIDI_SUFFIXES
        ),
        None,
    )
    if midi_entry is None:
        raise KfnSkipped("KFN пропущен: контейнер не содержит готовую MIDI-разметку нот")

    artist, title = _container_identity(
        container,
        parser,
        original_filename or source.name,
    )
    root = Path(output_root)
    root.mkdir(parents=True, exist_ok=True)
    config.UPLOAD_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kfn-midi-", dir=config.UPLOAD_TEMP_DIR) as temporary:
        bpm, key, duration, melody_track = _midi_document(
            midi_entry.payload, words, Path(temporary)
        )

    document = kar_dataset_service.KarDocument(
        title=title,
        artist=artist,
        bpm=bpm,
        key=key,
        duration=duration,
        words=words,
        lyric_track=-1,
        melody_track=melody_track,
        raw_lyrics=[],
    )
    kar_dataset_service._apply_known_identity(
        document, title=title_override, artist=artist_override
    )
    owns_target = target_dir is None
    if target_dir is None:
        target = kar_dataset_service._unique_dataset_dir(root, document)
    else:
        target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    try:
        symbolic_source = target / "source.kfn"
        if source.resolve() != symbolic_source.resolve():
            shutil.copy2(source, symbolic_source)
        kar_dataset_service._notify_dataset(
            progress, cancelled, "karaoke_parse", 8, "Разметка KFN прочитана"
        )
        with source.open("rb") as source_stream:
            digest = hashlib.file_digest(source_stream, "sha256").hexdigest()
        reference = kar_dataset_service._lyrics_payload(document, "kfn")
        audio_entry = _embedded_audio_entry(container)
        audio_ready = bool(audio_entry and _write_embedded_audio(audio_entry, target))
        warnings = [] if audio_ready else ["В KFN не найдена пригодная встроенная аудиодорожка"]
        comparison = {
            "status": "kfn-embedded-reference",
            "time_scale": 1.0,
            "offset_seconds": 0.0,
            "pitch_shift_semitones": 0,
        }
        stems_error: str | None = None
        stems_ready = False
        if audio_ready:
            try:
                kar_dataset_service._notify_dataset(
                    progress, cancelled, "separate", 36, "Разделяем голос и минусовку"
                )
                stems_ready = kar_dataset_service._prepare_fast_stems(target)
                kar_dataset_service._notify_dataset(
                    progress, cancelled, "separate", 68, "Голос и минусовка готовы"
                )
            except Exception as exc:
                stems_error = str(exc)
                warnings.append(f"Не удалось разделить оригинал на голос и минус: {exc}")
                (target / "vocals.flac").unlink(missing_ok=True)
                (target / "instrumental.flac").unlink(missing_ok=True)
        if stems_ready:
            try:
                refined = _refine_kfn_vocals(document, target)
                if refined is not None:
                    document, reference, comparison = refined
            except Exception as exc:
                warnings.append(f"Не удалось уточнить синхронизацию KFN по голосу: {exc}")
        media: dict[str, object] = {
            "cover_status": "fallback",
            "video_status": "fallback",
            "video_id": None,
            "warnings": [],
        }
        if audio_ready:
            kar_dataset_service._notify_dataset(
                progress, cancelled, "karaoke_media", 70, "Подготавливаем обложку и клип"
            )
            media = kar_dataset_service._prepare_visual_assets(
                target,
                title,
                artist,
                expected_duration=float(reference.get("duration") or 0) or None,
            )
            warnings.extend(map(str, media_warnings if isinstance((media_warnings := media.get("warnings", [])), list) else []))
            kar_dataset_service._notify_dataset(
                progress, cancelled, "karaoke_media", 97, "Визуальные файлы готовы"
            )
        write_json(target / "lyricsSync.json", reference)
        write_json(target / "comparison.json", comparison)
        note_count = sum(len(word.get("notes", [])) for word in reference["words"])
        metadata = {
            "dataset_version": 2,
            "status": "ready" if audio_ready else "review",
            "preparation_mode": "kfn-embedded-reference",
            "stems_status": "ready" if stems_ready else "error" if stems_error else "deferred",
            "title": title,
            "artist": artist,
            "bpm": bpm,
            "key": key,
            "duration": reference.get("duration", duration),
            "word_count": len(reference["words"]),
            "note_count": note_count,
            "kfn_sha256": digest,
            "original_filename": original_filename or source.name,
            "lyric_track": "Song.ini",
            "melody_track": melody_track,
            "container_variant": container.variant,
            "audio_source": {"kind": "kfn-embedded"} if audio_ready else None,
            "media": {key: value for key, value in media.items() if key != "warnings"},
            "alignment": comparison,
            "warnings": warnings,
            "files": sorted(item.name for item in target.iterdir() if item.is_file()),
        }
        write_json(target / "metadata.json", metadata)
        kar_dataset_service._notify_dataset(
            progress, cancelled, "complete", 100, "Песня готова к караоке"
        )
        return {**metadata, "dataset_dir": str(target.resolve())}
    except Exception:
        if owns_target:
            shutil.rmtree(target, ignore_errors=True)
        raise
