from __future__ import annotations

import html
import json
import queue
import re
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from difflib import SequenceMatcher

from .engines.text import normalize_lyrics_text

# Each individual request already has its own 8s socket timeout, but pisni.org.ua
# lookups can chain up to 8 detail-page fetches after the search page -- on a
# network that hangs (not fails fast) rather than being cleanly unreachable,
# that chain alone could take over a minute before falling back to ASR. This
# wall-clock budget bounds the whole lookup regardless of how many sources or
# candidate links it walks through.
LOOKUP_BUDGET_SECONDS = 20


@dataclass(frozen=True, slots=True)
class TimedLine:
    start: float
    text: str


@dataclass(frozen=True, slots=True)
class LyricsDiscovery:
    text: str
    source: str
    query: str
    language: str | None = None
    lines: tuple[TimedLine, ...] = ()


def _plain(value: str) -> str:
    return re.sub(r"^\s*\[\d{1,2}:\d{2}(?:\.\d+)?]\s*", "", value, flags=re.MULTILINE).strip()


def _timed(value: str) -> tuple[TimedLine, ...]:
    lines = []
    for row in value.splitlines():
        if match := re.match(r"^\s*\[(\d{1,2}):(\d{2}(?:\.\d+)?)\]\s*(.+?)\s*$", row):
            text = match[3].strip()
            if text:
                lines.append(TimedLine(int(match[1]) * 60 + float(match[2]), text))
    return tuple(lines)


def _trim_incomplete_repeated_tail(
    lines: tuple[TimedLine, ...],
) -> tuple[TimedLine, ...]:
    """Remove a provider-truncated final copy of an otherwise complete refrain.

    Synced-lyrics catalogs occasionally end with ``A, B, A, B, A, B...``
    where the last line is only a prefix of ``B``.  Passing that damaged copy
    to forced alignment invents timestamps for words which are not present in
    the recording.  Two complete adjacent copies are required as evidence, so
    an intentional one-off shortened ending is left untouched.
    """
    count = len(lines)
    for block_size in range(1, min(8, count // 3) + 1):
        first = lines[count - 3 * block_size:count - 2 * block_size]
        second = lines[count - 2 * block_size:count - block_size]
        tail = lines[count - block_size:]
        if [_identity(line.text) for line in first] != [
            _identity(line.text) for line in second
        ]:
            continue
        incomplete = False
        for expected, actual in zip(second, tail, strict=True):
            expected_tokens = _identity(expected.text).split()
            actual_tokens = _identity(actual.text).split()
            if not actual_tokens or actual_tokens != expected_tokens[:len(actual_tokens)]:
                break
            if len(actual_tokens) < len(expected_tokens):
                incomplete = True
        else:
            if incomplete:
                return lines[:-block_size]
    return lines


def _identity(value: str) -> str:
    value = normalize_lyrics_text(value)
    value = re.sub(r"\s*[\[(].*?[\])]\s*", " ", value)
    value = value.translate(str.maketrans({"i": "і", "I": "і"})).casefold()
    return " ".join(re.findall(r"[\w']+", value, flags=re.UNICODE))


def _lyrics_tokens(value: str) -> list[str]:
    return _identity(value).split()


def _select_complete_lyrics(
    candidates: tuple[LyricsDiscovery, ...] | list[LyricsDiscovery],
    *,
    minimum_coverage: float = 0.82,
) -> LyricsDiscovery | None:
    """Prefer a complete edition without accepting a different song version."""
    available = [candidate for candidate in candidates if candidate.text.strip()]
    if not available:
        return None
    anchor = available[0]
    anchor_tokens = _lyrics_tokens(anchor.text)
    selected = anchor
    selected_size = len(anchor_tokens)
    for candidate in available[1:]:
        candidate_tokens = _lyrics_tokens(candidate.text)
        if len(candidate_tokens) <= selected_size:
            continue
        matcher = SequenceMatcher(
            None,
            anchor_tokens,
            candidate_tokens,
            autojunk=False,
        )
        matched = sum(block.size for block in matcher.get_matching_blocks())
        coverage = matched / max(1, len(anchor_tokens))
        if coverage >= minimum_coverage:
            selected = candidate
            selected_size = len(candidate_tokens)
    return selected


def _lyrics_arrangement_candidates(
    lines: tuple[str, ...] | list[str],
) -> tuple[tuple[str, ...], ...]:
    """Build conservative alternatives for a catalog's repetitive outro.

    Lyrics providers sometimes publish one studio arrangement while another
    release reprises the opening verse near the end.  This function never
    chooses or publishes an alternative: it only offers structurally plausible
    candidates which the audio alignment stage can score later.
    """
    original = tuple(lines)
    count = len(original)
    candidates = [original]
    identities = tuple(_identity(line) for line in original)

    for block_size in range(1, min(4, count // 4) + 1):
        unit = identities[-block_size:]
        if not all(unit):
            continue

        repeats = 1
        cursor = count - 2 * block_size
        while cursor >= 0 and identities[cursor:cursor + block_size] == unit:
            repeats += 1
            cursor -= block_size
        if repeats < 4:
            continue

        suffix_start = count - repeats * block_size
        earlier_start = next(
            (
                index
                for index in range(suffix_start - block_size + 1)
                if identities[index:index + block_size] == unit
            ),
            None,
        )
        if earlier_start is None or earlier_start == 0:
            continue

        opening = original[:earlier_start]
        repeated_unit = original[-block_size:]
        retained_counts = sorted({1, max(1, repeats // 2), max(1, repeats - 2)})
        for retained in retained_counts:
            candidate = (
                original[:suffix_start]
                + repeated_unit * retained
                + opening
                + repeated_unit
            )
            if candidate not in candidates:
                candidates.append(candidate)
        break

    return tuple(candidates)


def _select_lyrics_arrangement(
    candidates: tuple[tuple[str, ...], ...] | list[tuple[str, ...]],
    heard_text: str,
    *,
    minimum_improvement: float = 0.04,
) -> tuple[str, ...]:
    """Select an arrangement only when an acoustic transcript supports it."""
    available = tuple(tuple(candidate) for candidate in candidates)
    if not available:
        return ()
    heard = _lyrics_tokens(heard_text)
    if len(heard) < 4:
        return available[0]

    def score(candidate: tuple[str, ...]) -> tuple[float, float]:
        expected = _lyrics_tokens("\n".join(candidate))
        if not expected:
            return 0.0, 0.0
        matcher = SequenceMatcher(None, expected, heard, autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        precision = matched / len(expected)
        recall = matched / len(heard)
        f1 = 2 * precision * recall / max(1e-9, precision + recall)
        return f1, recall

    baseline_score, _ = score(available[0])
    selected, selected_score = available[0], baseline_score
    for candidate in available[1:]:
        candidate_score, recall = score(candidate)
        if recall >= 0.55 and candidate_score > selected_score:
            selected, selected_score = candidate, candidate_score
    if selected is available[0] or selected_score < baseline_score + minimum_improvement:
        return available[0]
    return selected


def _select_scored_lyrics_arrangement(
    candidates: tuple[tuple[str, ...], ...] | list[tuple[str, ...]],
    acoustic_scores: tuple[float | None, ...] | list[float | None],
    *,
    minimum_improvement: float = 0.05,
) -> int:
    """Return the best candidate index when CTC likelihood clearly improves."""
    if not candidates or len(candidates) != len(acoustic_scores):
        return 0
    baseline = acoustic_scores[0]
    if baseline is None:
        return 0
    selected, selected_score = 0, float(baseline)
    for index, score in enumerate(acoustic_scores[1:], start=1):
        if score is not None and float(score) > selected_score:
            selected, selected_score = index, float(score)
    if selected_score < float(baseline) + minimum_improvement:
        return 0
    return selected


def _select_reprise_candidate_at_time(
    timed_lines: tuple[TimedLine, ...] | list[TimedLine],
    candidates: tuple[tuple[str, ...], ...] | list[tuple[str, ...]],
    reprise_time: float,
) -> int:
    """Map a detected musical reprise time to a generated lyric structure."""
    if len(candidates) < 2 or len(timed_lines) < 4:
        return 0
    original = tuple(line.text for line in timed_lines)
    identities = tuple(_identity(line) for line in original)
    count = len(original)
    for block_size in range(1, min(4, count // 4) + 1):
        unit = identities[-block_size:]
        repeats, cursor = 1, count - 2 * block_size
        while cursor >= 0 and identities[cursor:cursor + block_size] == unit:
            repeats += 1
            cursor -= block_size
        if repeats < 4:
            continue
        suffix_start = count - repeats * block_size
        earlier_start = next((index for index in range(suffix_start)
                              if identities[index:index + block_size] == unit), None)
        if earlier_start is None or earlier_start == 0:
            continue
        opening = original[:earlier_start]
        repeated_unit = original[-block_size:]
        best: tuple[float, int] | None = None
        for retained in sorted({1, max(1, repeats // 2), max(1, repeats - 2)}):
            predicted_line = suffix_start + retained * block_size
            if predicted_line >= len(timed_lines):
                continue
            candidate = (
                original[:suffix_start] + repeated_unit * retained
                + opening + repeated_unit
            )
            try:
                candidate_index = tuple(candidates).index(candidate)
            except ValueError:
                continue
            distance = abs(timed_lines[predicted_line].start - reprise_time)
            if best is None or distance < best[0]:
                best = distance, candidate_index
        if best is not None:
            return best[1]
    return 0


def _retime_arrangement_lines(
    timed_lines: tuple[TimedLine, ...] | list[TimedLine],
    selected_lines: tuple[str, ...] | list[str],
    *,
    duration: float,
) -> tuple[TimedLine, ...]:
    """Keep unchanged provider timings and estimate only a rearranged tail."""
    original = tuple(timed_lines)
    selected = tuple(selected_lines)
    common = 0
    while (
        common < len(original)
        and common < len(selected)
        and _identity(original[common].text) == _identity(selected[common])
    ):
        common += 1
    result = list(original[:common])
    if common >= len(selected):
        return tuple(result)
    if common < len(original):
        tail_start = original[common].start
    elif original:
        tail_start = original[-1].start
    else:
        tail_start = 0.0
    tail = selected[common:]
    weights = [max(1, len(_lyrics_tokens(line))) for line in tail]
    available = max(0.0, float(duration) - tail_start)
    total = max(1, sum(weights))
    consumed = 0
    for line, weight in zip(tail, weights, strict=True):
        result.append(TimedLine(tail_start + available * consumed / total, line))
        consumed += weight
    return tuple(result)


_CYRILLIC_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "ґ": "g", "д": "d",
    "е": "e", "ё": "e", "є": "ye", "ж": "zh", "з": "z", "и": "i",
    "і": "i", "ї": "yi", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu",
    "я": "ya",
})


def _latin_identity(value: str) -> str:
    return _identity(value).translate(_CYRILLIC_LATIN)


def _matches(expected: str, actual: str, threshold: float = .84) -> bool:
    left, right = _identity(expected), _identity(actual)
    if not (left and right):
        return False
    if left == right or SequenceMatcher(None, left, right).ratio() >= threshold:
        return True
    latin_left, latin_right = _latin_identity(left), _latin_identity(right)
    return SequenceMatcher(None, latin_left, latin_right).ratio() >= threshold


def _matches_recording(title: str, artist: str, row_artist: str, row_track: str) -> bool:
    return _matches(title, row_track) and (not artist or _matches(artist, row_artist))


def _lrclib_result(
    rows: object, *, title: str, artist: str, query: str
) -> LyricsDiscovery | None:
    for row in rows if isinstance(rows, list) else []:
        row_track = str(row.get("trackName") or "")
        row_artist = str(row.get("artistName") or "")
        if not _matches_recording(title, artist, row_artist, row_track):
            continue
        synced = row.get("syncedLyrics") or ""
        # When synchronized lyrics exist they are the canonical text for
        # alignment too. A provider's plainLyrics can differ by repeated
        # choruses or punctuation, making its line timestamps impossible to
        # map onto the otherwise similar plain transcript.
        timed_lines = _trim_incomplete_repeated_tail(_timed(synced))
        text = (
            "\n".join(line.text for line in timed_lines)
            if timed_lines else _plain(synced)
        ) or row.get("plainLyrics") or ""
        if str(text).strip():
            return LyricsDiscovery(
                str(text).strip(), "LRCLIB", query, lines=timed_lines
            )
    return None


def _request(url: str, encoding: str = "utf-8") -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "A&D-Voice/1"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.read().decode(encoding, errors="replace")


def _request_before(url: str, encoding: str, deadline: float) -> str:
    """Return a response before the lookup deadline, even on a stalled socket."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Lyrics lookup deadline expired")
    result: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, _request(url, encoding)))
        except Exception as exc:  # Propagate the request failure on the caller thread.
            result.put((False, exc))

    threading.Thread(target=run, name="lyrics-http", daemon=True).start()
    try:
        succeeded, value = result.get(timeout=remaining)
    except queue.Empty as exc:
        raise TimeoutError("Lyrics lookup deadline expired") from exc
    if not succeeded:
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("Lyrics request failed without an exception")
    return str(value)


def _expand_notation(value: str) -> str:
    lines, chorus, output, collecting = value.splitlines(), [], [], False
    for raw in lines:
        line = html.unescape(raw).strip()
        if re.fullmatch(r"(?:приспів|припев)\s*[:.]", line, flags=re.I):
            if line.endswith(":"):
                collecting = True
            elif chorus:
                output.extend(chorus)
            continue
        if not line:
            if collecting and chorus:
                collecting = False
            if output and output[-1]:
                output.append("")
            continue
        repeat = re.search(r"\|\s*\((\d+)\)\s*$", line)
        line = re.sub(r"\s*\|\s*\(\d+\)\s*$", "", line).strip()
        output.extend([line] * (int(repeat.group(1)) if repeat else 1))
        if collecting:
            chorus.append(line)
    return "\n".join(output).strip()


def _pisni(artist: str, track: str, query: str, deadline: float) -> LyricsDiscovery | None:
    try:
        encoded = urllib.parse.quote_from_bytes(track.encode("cp1251"))
        page = _request_before(
            f"https://www.pisni.org.ua/search.php?phrase={encoded}&obj=s", "cp1251", deadline
        )
        links = dict.fromkeys(re.findall(r'href=["\'](/songs/\d+\.html)["\']', page, re.I))
        for link in list(links)[:8]:
            if time.monotonic() >= deadline:
                break
            detail = _request_before(f"https://www.pisni.org.ua{link}", "cp1251", deadline)
            title = re.search(r'<h1[^>]*>(.*?)</h1>', detail, re.I | re.S)
            performer = re.search(r'<a href=["\']/persons/[^"\']+["\'][^>]*>(.*?)</a>', detail, re.I | re.S)
            lyrics = re.search(r'<pre class=["\']songwords["\']>(.*?)</pre>', detail, re.I | re.S)
            if not (title and performer and lyrics):
                continue
            def clean(match):
                return html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).strip()

            if _matches(track, clean(title)) and (not artist or _matches(artist, clean(performer))):
                text = _expand_notation(clean(lyrics))
                if len(text.split()) >= 15:
                    return LyricsDiscovery(text, "pisni.org.ua", query)
    except (OSError, UnicodeError, ValueError):
        pass
    return None


def _musixmatch(
    artist: str,
    track: str,
    query: str,
    deadline: float,
) -> LyricsDiscovery | None:
    if not artist or time.monotonic() >= deadline:
        return None
    url = "https://www.musixmatch.com/lyrics/{}/{}".format(
        urllib.parse.quote(artist, safe=""),
        urllib.parse.quote(track, safe=""),
    )
    try:
        page = _request_before(url, "utf-8", deadline)
        try:
            payload = json.loads(page)
            body = str(payload.get("lyrics", {}).get("body") or "")
        except (TypeError, ValueError):
            match = re.search(
                r'"lyrics"\s*:\s*\{\s*"body"\s*:\s*("(?:\\.|[^"\\])*")',
                page,
            )
            body = str(json.loads(match.group(1))) if match else ""
        body = body.strip()
        if len(_lyrics_tokens(body)) >= 10:
            return LyricsDiscovery(body, "Musixmatch", query)
    except (OSError, TimeoutError, TypeError, ValueError):
        pass
    return None


def discover_lyrics(
    title: str | None,
    artist: str | None = None,
    *_args,
    complete: bool = False,
    duration_seconds: float | None = None,
    **_kwargs,
) -> LyricsDiscovery | None:
    track = " ".join(str(title or "").replace("_", " ").split())
    performer = " ".join(str(artist or "").replace("_", " ").split())
    if not track:
        return None
    query = f"{performer} - {track}" if performer else track
    deadline = time.monotonic() + LOOKUP_BUDGET_SECONDS
    if performer and duration_seconds is not None and float(duration_seconds) > 0:
        exact_params = {
            "track_name": track,
            "artist_name": performer,
            "duration": round(float(duration_seconds)),
        }
        exact_url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(exact_params)
        try:
            exact_row = json.loads(_request_before(exact_url, "utf-8", deadline))
        except (OSError, TypeError, ValueError):
            exact_row = None
        if isinstance(exact_row, dict) and (
            result := _lrclib_result(
                [exact_row],
                title=track,
                artist=performer,
                query=query,
            )
        ):
            if complete:
                alternate = _musixmatch(performer, track, query, deadline)
                return _select_complete_lyrics((result, alternate)) if alternate else result
            return result
    params = {"track_name": track, "artist_name": performer} if performer else {"track_name": track}
    url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(params)
    try:
        rows = json.loads(_request_before(url, "utf-8", deadline))
    except (OSError, ValueError):
        rows = []
    if result := _lrclib_result(
        rows, title=track, artist=performer, query=query
    ):
        if complete:
            alternate = _musixmatch(performer, track, query, deadline)
            return _select_complete_lyrics((result, alternate)) if alternate else result
        return result
    # Providers sometimes romanize the artist (for example a Cyrillic name
    # stored in Latin script). Search by the exact title only, then still
    # verify both returned fields against the caller's exact metadata.
    if performer:
        if time.monotonic() >= deadline:
            return None
        title_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(
            {"track_name": track}
        )
        try:
            title_rows = json.loads(_request_before(title_url, "utf-8", deadline))
        except (OSError, ValueError):
            title_rows = []
        if result := _lrclib_result(
            title_rows, title=track, artist=performer, query=query
        ):
            return result
    if time.monotonic() >= deadline:
        return None
    if complete:
        if result := _musixmatch(performer, track, query, deadline):
            return result
        if time.monotonic() >= deadline:
            return None
    return _pisni(performer, track, query, deadline)
