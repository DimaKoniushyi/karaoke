from __future__ import annotations

from ..models import Word


def _repair_duplicate_onsets(
    words: list[Word],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    for line_index, (_line_start, lower, upper) in enumerate(entries):
        position = lower
        line_end = (
            min(span, entries[line_index + 1][0])
            if line_index + 1 < len(entries) else span
        )
        while position < upper - 1:
            run_end = position + 1
            while (
                run_end < upper
                and abs(words[run_end].start - words[position].start) <= 1e-6
            ):
                run_end += 1
            if run_end == position + 1:
                position += 1
                continue
            boundary = (
                words[run_end].start
                if run_end < upper else min(
                    line_end,
                    max(word.end for word in words[position:run_end]),
                )
            )
            if boundary <= words[position].start + 0.01 * (run_end - position):
                position = run_end
                continue
            weights = [
                max(1, sum(character.isalnum() for character in token))
                for token in tokens[position:run_end]
            ]
            remaining = sum(weights)
            cursor = words[position].start
            for index, weight in zip(
                range(position, run_end), weights, strict=True
            ):
                end = (
                    boundary
                    if index == run_end - 1
                    else cursor + (boundary - cursor) * weight / remaining
                )
                original = words[index]
                words[index] = Word(
                    cursor,
                    end,
                    original.text,
                    min(original.confidence, 0.5),
                    original.index,
                )
                cursor = end
                remaining -= weight
            position = run_end


def _repair_final_preposition_words(
    words: list[Word],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    short_prepositions = {"в", "с", "к", "з"}
    for line_index, (_line_start, lower, upper) in enumerate(entries):
        if upper - lower < 2 or line_index + 1 >= len(entries):
            continue
        line_end = min(span, entries[line_index + 1][0])
        preposition_index, lexical_index = upper - 2, upper - 1
        preposition = "".join(
            char for char in tokens[preposition_index].casefold() if char.isalnum()
        )
        left, right = words[preposition_index], words[lexical_index]
        if (
            preposition in short_prepositions
            and right.start >= line_end - 0.15
            and right.start - left.start >= 1.0
        ):
            words[lexical_index] = Word(
                left.start,
                max(left.start + 0.01, min(line_end, right.end)),
                right.text,
                0.0,
                right.index,
            )


def _line_identity(tokens: list[str]) -> tuple[str, ...]:
    return tuple(
        "".join(char for char in token.casefold() if char.isalnum())
        for token in tokens
    )


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    middle = len(ordered) // 2
    return (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2
    )


def _repair_repeated_line_transitions(
    words: list[Word],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    transitions: dict[
        tuple[tuple[str, ...], tuple[str, ...]],
        list[tuple[int, float]],
    ] = {}
    for index in range(len(entries) - 1):
        _start, lower, upper = entries[index]
        _next_start, next_lower, next_upper = entries[index + 1]
        key = (
            _line_identity(tokens[lower:upper]),
            _line_identity(tokens[next_lower:next_upper]),
        )
        transitions.setdefault(key, []).append(
            (index, words[next_lower].start - words[lower].start)
        )
    for occurrences in transitions.values():
        if len(occurrences) < 3:
            continue
        typical = _median([delta for _index, delta in occurrences])
        if typical <= 0.2:
            continue
        for line_index, delta in occurrences:
            if delta >= typical * 0.45:
                continue
            _start, lower, _upper = entries[line_index]
            _next_start, next_lower, next_upper = entries[line_index + 1]
            target = words[lower].start + typical
            group = words[next_lower:next_upper]
            boundary = (
                min(span, words[entries[line_index + 2][1]].start)
                if line_index + 2 < len(entries)
                else span
            )
            if target >= boundary:
                continue
            source_start = group[0].start
            source_span = max(0.01, group[-1].end - source_start)
            scale = min(1.0, (boundary - target) / source_span)
            words[next_lower:next_upper] = [
                Word(
                    target + (word.start - source_start) * scale,
                    target + (word.end - source_start) * scale,
                    word.text,
                    min(word.confidence, 0.5),
                    word.index,
                )
                for word in group
            ]


def _repeated_shape_is_outlier(template: list[Word], group: list[Word]) -> bool:
    template_span = template[-1].start - template[0].start
    current_span = group[-1].start - group[0].start
    template_duration = template[-1].end - template[0].start
    current_duration = group[-1].end - group[0].start
    if template_span <= 0:
        return False
    relative_error = max(
        abs(
            (current.start - group[0].start)
            - (expected.start - template[0].start)
        )
        for expected, current in zip(template, group, strict=True)
    )
    return (
        relative_error > max(1.0, template_span * 0.3)
        or not 0.6 <= current_span / template_span <= 1.6
        or current_duration > max(
            template_duration * 2.5,
            template_duration + 2.0,
        )
    )


def _repair_repeated_line_shapes(
    words: list[Word],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    templates: dict[tuple[str, ...], list[Word]] = {}
    for line_index, (_line_start, lower, upper) in enumerate(entries):
        identity = _line_identity(tokens[lower:upper])
        group = words[lower:upper]
        template = templates.get(identity)
        if template is None:
            prefix_templates = [
                (
                    sum(
                        expected != current
                        for expected, current in zip(
                            candidate_identity[:len(identity)],
                            identity,
                            strict=True,
                        )
                    ),
                    len(candidate_identity),
                    candidate[:len(identity)],
                )
                for candidate_identity, candidate in templates.items()
                if (
                    len(candidate_identity) > len(identity)
                    and sum(
                        expected != current
                        for expected, current in zip(
                            candidate_identity[:len(identity)],
                            identity,
                            strict=True,
                        )
                    ) <= 1
                )
            ]
            if prefix_templates:
                template = min(prefix_templates, key=lambda item: item[:2])[2]
        if template is None:
            templates[identity] = list(group)
            continue
        if len(group) < 2 or not _repeated_shape_is_outlier(template, group):
            continue
        line_end = (
            min(span, entries[line_index + 1][0])
            if line_index + 1 < len(entries) else span
        )
        anchor = group[0].start
        repaired = []
        for expected, current in zip(template, group, strict=True):
            start = anchor + expected.start - template[0].start
            end = anchor + expected.end - template[0].start
            if start >= line_end:
                repaired = []
                break
            repaired.append(Word(
                start,
                max(start + 0.01, min(line_end, end)),
                current.text,
                min(current.confidence, expected.confidence),
                current.index,
            ))
        if repaired:
            words[lower:upper] = repaired
            templates[identity] = list(repaired)


def _repair_repeated_single_word_durations(
    words: list[Word],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    occurrences: dict[tuple[str, ...], list[tuple[int, float]]] = {}
    for line_index, (_line_start, lower, upper) in enumerate(entries):
        if upper - lower != 1:
            continue
        line_end = (
            min(span, words[entries[line_index + 1][1]].start)
            if line_index + 1 < len(entries)
            else span
        )
        word = words[lower]
        duration = min(word.end, line_end) - word.start
        occurrences.setdefault(_line_identity(tokens[lower:upper]), []).append(
            (lower, duration)
        )
    for repeated in occurrences.values():
        stable = sorted(duration for _index, duration in repeated if duration >= 0.2)
        if len(repeated) < 2 or not stable:
            continue
        typical = _median(stable)
        for index, measured in repeated:
            if measured >= 0.2:
                continue
            word = words[index]
            line_index = next(
                position
                for position, (_start, lower, upper) in enumerate(entries)
                if lower <= index < upper
            )
            line_end = (
                min(span, words[entries[line_index + 1][1]].start)
                if line_index + 1 < len(entries)
                else span
            )
            words[index] = Word(
                word.start,
                min(line_end, word.start + typical),
                word.text,
                min(word.confidence, 0.5),
                word.index,
            )


def repair_timed_line_outliers(
    words: list[Word],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    """Repair gross CTC line geometry using evidence from the same recording."""
    _repair_duplicate_onsets(words, entries, tokens, span)
    _repair_final_preposition_words(words, entries, tokens, span)
    _repair_repeated_line_transitions(words, entries, tokens, span)
    _repair_repeated_single_word_durations(words, entries, tokens, span)
    _repair_repeated_line_shapes(words, entries, tokens, span)
