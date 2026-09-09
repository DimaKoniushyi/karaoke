from __future__ import annotations

from ..models import Word


def coarse_line_starts(
    token_counts: list[int],
    voice_intervals: list[tuple[float, float]],
    *,
    span: float,
) -> list[float]:
    """Place lyric lines along cumulative vocal time, skipping silent gaps."""
    counts = [max(0, int(count)) for count in token_counts]
    if not counts:
        return []
    intervals: list[tuple[float, float]] = []
    for raw_start, raw_end in sorted(voice_intervals):
        start = max(0.0, min(float(span), float(raw_start)))
        end = max(start, min(float(span), float(raw_end)))
        if end <= start:
            continue
        if intervals and start <= intervals[-1][1]:
            intervals[-1] = intervals[-1][0], max(intervals[-1][1], end)
        else:
            intervals.append((start, end))
    if not intervals:
        intervals = [(0.0, max(0.0, float(span)))]
    active_duration = sum(end - start for start, end in intervals)
    total_tokens = max(1, sum(counts))

    def at_active_offset(offset: float) -> float:
        remaining = min(max(0.0, offset), active_duration)
        for start, end in intervals:
            duration = end - start
            if remaining <= duration:
                return start + remaining
            remaining -= duration
        return intervals[-1][1]

    starts, consumed = [], 0
    for count in counts:
        starts.append(at_active_offset(active_duration * consumed / total_tokens))
        consumed += count
    return starts


def _invalid(word: Word, span: float) -> bool:
    return word.start < 0 or word.end <= word.start or word.end > span + 0.1


def invalid_runs(words: list[Word], span: float) -> list[tuple[int, int]]:
    indices = [index for index, word in enumerate(words) if _invalid(word, span)]
    return runs(indices)


def runs(indices: list[int]) -> list[tuple[int, int]]:
    result: list[list[int]] = []
    for index in sorted(set(indices)):
        if not result or index != result[-1][1]:
            result.append([index, index + 1])
        else:
            result[-1][1] = index + 1
    return [(start, end) for start, end in result]


def _longest_false_run(mask) -> int:
    import numpy as np

    if not len(mask):
        return 0
    padded = np.concatenate(([True], mask, [True]))
    edges = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(edges == -1)
    if not len(starts):
        return 0
    ends = np.flatnonzero(edges == 1)
    return int(np.max(ends - starts))


def acoustic_runs(words: list[Word], samples, rate: int) -> list[tuple[int, int]]:
    import numpy as np

    bad = {
        index
        for index in range(len(words) - 1)
        if words[index].end > words[index + 1].start + 0.04
    }
    bad.update(index + 1 for index in tuple(bad))
    mono = np.asarray(samples, dtype=np.float32)
    if mono.ndim > 1:
        mono = mono.mean(axis=1)
    frame = max(1, round(rate * 0.02))
    usable = len(mono) // frame * frame
    if usable < frame:
        return runs(list(bad))
    rms = np.sqrt(np.mean(mono[:usable].reshape(-1, frame) ** 2, axis=1))
    audible = rms[rms > 1e-7]
    if not len(audible):
        return runs(list(bad))
    floor = float(np.percentile(audible, 15))
    signal = float(np.percentile(audible, 85))
    threshold = max(min(floor * 2.5, signal * 0.25), signal * 0.025)
    active = rms >= threshold
    for index, word in enumerate(words):
        word_duration = word.end - word.start
        if word_duration < 0.45:
            continue
        lower = max(0, round(word.start * rate / frame))
        upper = min(len(active), max(lower + 1, round(word.end * rate / frame)))
        silent_duration = _longest_false_run(active[lower:upper]) * frame / rate
        if silent_duration >= min(0.55, word_duration * 0.45):
            bad.add(index)
    return runs(list(bad))


def context_groups(
    invalid: list[tuple[int, int]], count: int
) -> list[tuple[int, int]]:
    groups: list[tuple[int, int]] = []
    for start, end in invalid:
        lower, upper = max(0, start - 2), min(count, end + 2)
        if groups and lower <= groups[-1][1]:
            groups[-1] = groups[-1][0], max(groups[-1][1], upper)
        else:
            groups.append((lower, upper))
    return groups


def repair_bounds(
    words: list[Word], start: int, end: int, span: float, context: int = 2
):
    left = next(
        (index for index in range(start - 1, -1, -1) if not _invalid(words[index], span)),
        None,
    )
    right = next(
        (index for index in range(end, len(words)) if not _invalid(words[index], span)),
        None,
    )
    if left is None and right is None:
        return 0, len(words), 0.0, span, left, right
    estimate = max(4.0, (end - start) * 1.25)
    lower = max(0, start - (context if left is not None else 0))
    upper = min(len(words), end + (context if right is not None else 0))
    if left is not None and right is not None:
        crop_start = min(words[left].start, words[right].start) - 1
        crop_end = max(words[left].end, words[right].end) + 1
    elif left is not None:
        crop_start, crop_end = words[left].start - 1, words[left].end + estimate
    else:
        crop_start, crop_end = words[right].start - estimate, words[right].end + 1
    crop_start, crop_end = max(0, crop_start), min(span, crop_end)
    if crop_end - crop_start < 1.0:
        crop_start = max(0.0, crop_end - 1.0)
        crop_end = min(span, crop_start + 1.0)
    return lower, upper, crop_start, crop_end, left, right


def enforce_monotonic_starts(words: list[Word], span: float) -> None:
    for index in range(1, len(words)):
        if words[index].start + 1e-6 < words[index - 1].start:
            new_start = min(words[index - 1].start, span)
            new_end = (
                words[index].end
                if words[index].end > new_start
                else min(span, new_start + 0.05)
            )
            words[index] = Word(
                new_start,
                new_end,
                words[index].text,
                words[index].confidence,
                words[index].index,
            )


def repair_collapsed_timed_lines(
    words: list[Word], entries: list[tuple[float, int, int]], span: float
) -> None:
    for line_index, (line_start, lower, upper) in enumerate(entries):
        if upper - lower < 3:
            continue
        line_end = entries[line_index + 1][0] if line_index + 1 < len(entries) else span
        window_start, window_end = max(0.0, line_start), min(span, line_end)
        window_span = window_end - window_start
        if window_span < 1.0:
            continue
        group = words[lower:upper]
        measured_span = max(word.end for word in group) - min(word.start for word in group)
        if measured_span >= min(1.0, window_span * 0.35):
            continue
        weights = [max(1, sum(char.isalnum() for char in word.text)) for word in group]
        cursor = window_start
        for index, (word, weight) in enumerate(
            zip(group, weights, strict=True), start=lower
        ):
            boundary = (
                window_end
                if index == upper - 1
                else cursor
                + (window_end - cursor) * weight / sum(weights[index - lower :])
            )
            words[index] = Word(cursor, boundary, word.text, 0.0, word.index)
            cursor = boundary


def fill_unresolved_timed_lines(
    words: list[Word | None],
    entries: list[tuple[float, int, int]],
    tokens: list[str],
    span: float,
) -> None:
    for line_index, (line_start, lower, upper) in enumerate(entries):
        if all(word is not None for word in words[lower:upper]):
            continue
        line_end = entries[line_index + 1][0] if line_index + 1 < len(entries) else span
        window_start, window_end = max(0.0, line_start), min(span, line_end)
        if window_end <= window_start:
            continue
        index = lower
        while index < upper:
            if words[index] is not None:
                index += 1
                continue
            run_start = index
            while index < upper and words[index] is None:
                index += 1
            run_end = index
            left_word = words[run_start - 1] if run_start > lower else None
            right_word = words[run_end] if run_end < upper else None
            start = max(
                window_start,
                left_word.end if left_word is not None else window_start,
            )
            end = min(
                window_end,
                right_word.start if right_word is not None else window_end,
            )
            if end <= start:
                run_start, run_end = lower, upper
                start, end = window_start, window_end
            weights = [
                max(1, sum(char.isalnum() for char in token))
                for token in tokens[run_start:run_end]
            ]
            remaining = sum(weights)
            cursor = start
            for word_index, weight in zip(
                range(run_start, run_end), weights, strict=True
            ):
                boundary = (
                    end
                    if word_index == run_end - 1
                    else cursor + (end - cursor) * weight / remaining
                )
                words[word_index] = Word(
                    cursor, boundary, tokens[word_index], 0.0, word_index
                )
                cursor = boundary
                remaining -= weight


def timed_line_retry_stages(words, entries, span: float):
    per_line = []
    for index, (start, lower, upper) in enumerate(entries):
        if any(word is None for word in words[lower:upper]):
            end = entries[index + 1][0] if index + 1 < len(entries) else span
            per_line.append((lower, upper, max(0, start - 0.5), min(span, end + 0.5)))
    yield per_line

    contexts = []
    for index, (_, lower, upper) in enumerate(entries):
        if any(word is None for word in words[lower:upper]):
            first, last = max(0, index - 1), min(len(entries), index + 2)
            start = max(0, entries[first][0] - 1)
            end = min(span, (entries[last][0] if last < len(entries) else span) + 1)
            contexts.append((entries[first][1], entries[last - 1][2], start, end))
    yield contexts

    triads = []
    for _, lower, upper in entries:
        for index in range(lower + 1, upper - 1):
            if (
                words[index] is None
                and words[index - 1] is not None
                and words[index + 1] is not None
            ):
                start = max(0, words[index - 1].start - 0.5)
                end = min(span, words[index + 1].end + 0.5)
                triads.append((index - 1, index + 2, start, end))
    yield triads

    singles = []
    for _, lower, upper in entries:
        for index in range(lower + 1, upper - 1):
            if (
                words[index] is None
                and words[index - 1] is not None
                and words[index + 1] is not None
            ):
                center = (words[index - 1].end + words[index + 1].start) / 2
                start = max(words[index - 1].start, center - 1)
                end = min(words[index + 1].end, center + 1)
                if end > start:
                    singles.append((index, index + 1, start, end))
    yield singles

    wide_singles = []
    for line_index, (line_start, lower, upper) in enumerate(entries):
        line_end = entries[line_index + 1][0] if line_index + 1 < len(entries) else span
        for index in range(lower, upper):
            if words[index] is None:
                wide_singles.append(
                    (
                        index,
                        index + 1,
                        max(0, line_start - 0.75),
                        min(span, line_end + 0.75),
                    )
                )
    yield wide_singles


def ctc_windows(
    entries: list[tuple[float, int, int]],
    span: float,
    *,
    tokens: list[str] | tuple[str, ...] = (),
    max_window_seconds: float = 10.0,
    max_tokens: int = 64,
) -> list[tuple[float, float, int, int]]:
    """Batch adjacent lyric lines without creating unsafe full-song tensors."""
    windows: list[tuple[float, float, int, int]] = []
    line_index = 0
    while line_index < len(entries):
        first = line_index
        line_start, lower, upper = entries[first]
        line_index += 1
        while line_index < len(entries):
            _candidate_start, candidate_lower, candidate_upper = entries[line_index]
            _previous_start, previous_lower, previous_upper = entries[line_index - 1]
            if tokens and tuple(tokens[previous_lower:previous_upper]) == tuple(
                tokens[candidate_lower:candidate_upper]
            ):
                # Consecutive copies of the same refrain are acoustically
                # ambiguous inside one CTC target. Give each occurrence its
                # own provider-bounded window so it cannot claim its neighbour.
                break
            following_start = (
                entries[line_index + 1][0]
                if line_index + 1 < len(entries)
                else span
            )
            candidate_end = min(span, following_start + 0.75)
            window_start = max(0.0, line_start - 0.6)
            if (
                candidate_end - window_start > max_window_seconds
                or candidate_upper - lower > max_tokens
            ):
                break
            upper = candidate_upper
            line_index += 1
        following_start = entries[line_index][0] if line_index < len(entries) else span
        window_start = max(0.0, line_start - 0.6)
        window_end = min(
            span,
            max(line_start + 0.75, min(following_start + 0.75, window_start + max_window_seconds)),
        )
        windows.append((window_start, window_end, lower, upper))
    return windows
