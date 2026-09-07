from __future__ import annotations

from pathlib import Path
from typing import Any

from AI.lyrics_document import flatten_word_notes, replace_word_notes, validate_lyrics_document
from app.utils.json_files import read_json, write_json

JsonObject = dict[str, Any]


def _load(output_dir: Path) -> JsonObject:
    payload: Any = read_json(Path(output_dir) / "lyricsSync.json", default={})
    return validate_lyrics_document(payload)


def _write(output_dir: Path, payload: JsonObject) -> None:
    output_dir = Path(output_dir)
    write_json(output_dir / "lyricsSync.json", validate_lyrics_document(payload))


def load_editor(output_dir: Path) -> tuple[JsonObject, bool]:
    payload = _load(output_dir)
    editor = payload.get("editor")
    backup_exists = isinstance(editor, dict) and isinstance(editor.get("ai_notes"), list)
    return payload, backup_exists


def normalize_editor_timeline(payload: JsonObject) -> JsonObject:
    return validate_lyrics_document(payload)


def save_editor(
    output_dir: Path,
    raw_notes: list[JsonObject],
    word_texts: list[str] | None = None,
    word_bounds: list[JsonObject] | None = None,
) -> JsonObject:
    payload = _load(output_dir)
    editor_value = payload.get("editor")
    editor: JsonObject = editor_value if isinstance(editor_value, dict) else {}
    if not isinstance(editor.get("ai_notes"), list):
        editor["ai_notes"] = [
            [dict(note) for note in word["notes"]] for word in payload["words"]
        ]
    if word_texts is not None and len(word_texts) != len(payload["words"]):
        raise ValueError("word_texts length must match the number of words")
    if word_bounds is not None and len(word_bounds) != len(payload["words"]):
        raise ValueError("word_bounds length must match the number of words")
    if word_texts is not None or word_bounds is not None:
        words = payload["words"]
        texts = word_texts if word_texts is not None else [None] * len(words)
        bounds = word_bounds if word_bounds is not None else [None] * len(words)
        payload = {
            **payload,
            "words": [
                {
                    **word,
                    **({"text": text} if text is not None else {}),
                    **({"start": float(bound["start"]), "end": float(bound["end"])} if bound is not None else {}),
                }
                for word, text, bound in zip(words, texts, bounds, strict=True)
            ],
        }
    updated = replace_word_notes(payload, raw_notes)
    updated["editor"] = {**editor, "edited": True, "source": "manual"}
    _write(output_dir, updated)
    return updated


def reset_editor(output_dir: Path) -> JsonObject:
    payload = _load(output_dir)
    editor = payload.get("editor")
    backups = editor.get("ai_notes") if isinstance(editor, dict) else None
    if not isinstance(backups, list) or len(backups) != len(payload["words"]):
        raise ValueError("AI note backup is not available")
    restored = {
        **payload,
        "words": [
            {**word, "notes": [dict(note) for note in backups[index]]}
            for index, word in enumerate(payload["words"])
        ],
        "editor": {"edited": False, "source": "ai"},
    }
    _write(output_dir, restored)
    return restored


def editor_notes(payload: JsonObject) -> list[JsonObject]:
    return flatten_word_notes(payload)
