from __future__ import annotations

import difflib
from typing import Any


def carry_forward_word_notes(
    previous: list[Any], words: list[dict[str, Any]]
) -> None:
    """Keep notes on unchanged words after a lyrics edit."""
    previous_texts = [
        str(word.get("text", "")).strip().casefold()
        if isinstance(word, dict)
        else ""
        for word in previous
    ]
    new_texts = [str(word.get("text", "")).strip().casefold() for word in words]
    matcher = difflib.SequenceMatcher(
        None, previous_texts, new_texts, autojunk=False
    )
    for tag, previous_start, previous_end, new_start, _new_end in matcher.get_opcodes():
        if tag != "equal":
            continue
        for offset in range(previous_end - previous_start):
            old = previous[previous_start + offset]
            word = words[new_start + offset]
            same_interval = (
                isinstance(old, dict)
                and old.get("start") == word.get("start")
                and old.get("end") == word.get("end")
            )
            if same_interval:
                word["notes"] = [dict(note) for note in old.get("notes", [])]
