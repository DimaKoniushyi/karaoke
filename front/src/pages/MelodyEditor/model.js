import { clamp } from "../../utils/math";

const sort = (a, b) => a.start - b.start || a.note - b.note;

export const roundTime = (n) => Math.round(n * 1000) / 1000;
export const cloneNotes = (notes) => notes.map((n) => ({ ...n }));

// Moves the playhead DOM node directly instead of through React state --
// this runs every animation frame during playback, and re-rendering the
// editor at 60fps would be far more work than a transform assignment.
export function setPlayheadPosition(ref, time, zoom, { announce = true } = {}) {
  const node = ref?.current;
  if (!node) return;
  node.style.transform = `translate3d(${time * zoom}px, 0, 0) translateX(-50%)`;
  if (announce) node.setAttribute("aria-valuenow", String(time));
}

export function normalizeNotes(notes = []) {
  const ends = new Map();

  return notes
    .map((n, i) => ({
      ...n,
      _id: n._id || `note-${i}-${n.start}-${n.end}`,
      note: +n.note,
      start: Math.max(+n.word_start, +n.start),
      end: Math.min(+n.word_end, +n.end),
      word_index: +n.word_index,
      word_start: +n.word_start,
      word_end: +n.word_end
    }))
    .filter(
      (n) =>
        Number.isInteger(n.note) &&
        n.note >= 0 &&
        n.note <= 127 &&
        Number.isInteger(n.word_index) &&
        n.end > n.start
    )
    .sort((a, b) => a.word_index - b.word_index || sort(a, b))
    .filter((n) => {
      if (n.start < (ends.get(n.word_index) ?? n.word_start)) return false;
      ends.set(n.word_index, n.end);
      return true;
    })
    .sort(sort);
}

const selectedNotes = (notes, ids) => {
  ids = new Set(ids);
  return notes.filter((n) => ids.has(n._id)).sort(sort);
};

export function recombineAdjacentEqualPitchNotes(notes, epsilon = 0.01) {
  const result = [];
  for (const n of [...notes].sort(sort)) {
    const prev = result.at(-1);
    if (prev?.note === n.note && n.start - prev.end <= epsilon) {
      prev.end = Math.max(prev.end, n.end);
      prev.word_start = Math.min(prev.word_start, n.word_start);
      prev.word_end = Math.max(prev.word_end, n.word_end);
    } else {
      result.push({ ...n });
    }
  }
  return result;
}

const contiguous = (notes, chosen) => {
  if (chosen.length < 2) return false;
  const ids = new Set(chosen.map((n) => n._id));
  const indexes = [...notes]
    .sort(sort)
    .map((n, i) => (ids.has(n._id) ? i : -1))
    .filter((i) => i >= 0);
  return indexes.at(-1) - indexes[0] + 1 === indexes.length;
};

export function mergeSelectedNotes(notes, ids) {
  const chosen = selectedNotes(notes, ids);
  if (!contiguous(notes, chosen)) {
    return { notes, selectedId: chosen[0]?._id || null };
  }
  ids = new Set(ids);
  const merged = {
    ...chosen[0],
    start: chosen[0].start,
    end: chosen.at(-1).end,
    note: Math.round(chosen.reduce((sum, n) => sum + n.note, 0) / chosen.length),
    word_start: Math.min(...chosen.map((n) => n.word_start)),
    word_end: Math.max(...chosen.map((n) => n.word_end))
  };
  return {
    notes: normalizeNotes([...notes.filter((n) => !ids.has(n._id)), merged]),
    selectedId: merged._id
  };
}

export const canMergeSelectedNotes = (notes, ids) => contiguous(notes, selectedNotes(notes, ids));

export const deleteNotes = (notes, ids) => {
  ids = new Set(ids);
  return notes.filter((n) => !ids.has(n._id));
};

export function adjacentNoteId(notes, ids, direction) {
  const ordered = [...notes].sort(sort);
  if (!ordered.length) return null;
  ids = new Set(ids);
  const index =
    direction > 0
      ? ordered.findIndex((n) => ids.has(n._id))
      : ordered.findLastIndex((n) => ids.has(n._id));
  return index < 0
    ? (direction > 0 ? ordered[0] : ordered.at(-1))._id
    : ordered[clamp(index + Math.sign(direction), 0, ordered.length - 1)]._id;
}

export function constrainedMoveDelta(notes, ids, requested) {
  ids = new Set(ids);
  const chosen = notes.filter((n) => ids.has(n._id));
  if (!chosen.length) return 0;
  let min = Math.max(...chosen.map((n) => n.word_start - n.start));
  let max = Math.min(...chosen.map((n) => n.word_end - n.end));
  for (const current of chosen) {
    for (const n of notes) {
      if (ids.has(n._id) || n.word_index !== current.word_index) continue;
      if (n.end <= current.start) min = Math.max(min, n.end - current.start);
      if (n.start >= current.end) max = Math.min(max, n.start - current.end);
    }
  }
  return clamp(requested, min, max);
}

export function resizeBounds(notes, id, duration = 0.03) {
  const current = notes.find((n) => n._id === id);
  if (!current) return null;
  let minStart = current.word_start;
  let maxEnd = current.word_end;
  for (const n of notes) {
    if (n === current || n.word_index !== current.word_index) continue;
    if (n.end <= current.start) minStart = Math.max(minStart, n.end);
    if (n.start >= current.end) maxEnd = Math.min(maxEnd, n.start);
  }
  return { minStart, maxStart: current.end - duration, minEnd: current.start + duration, maxEnd };
}

export const canonicalLyricProjection = (words = []) =>
  words.map((w, index) => ({ index, text: `${w.text || ""}`, start: +w.start, end: +w.end }));

export function wordResizeBounds(words, index, duration, minDuration = 0.05) {
  const word = words[index];
  if (!word) return null;
  return {
    minStart: words[index - 1]?.start ?? 0,
    maxStart: Math.min(word.end - minDuration, words[index + 1]?.start ?? duration),
    minEnd: word.start + minDuration,
    maxEnd: duration
  };
}

export function notesOverlappingWords(notes, words, indexes) {
  const ranges = indexes.map((i) => words[i]).filter(Boolean);
  return new Set(
    notes.filter((n) => ranges.some((w) => n.end > w.start && n.start < w.end)).map((n) => n._id)
  );
}

export function visibleTimeRange({ scrollLeft, clientWidth }, { keyboardWidth, zoom }) {
  const pad = Math.max(480, clientWidth / 2);
  const x = scrollLeft - keyboardWidth;
  return [(x - pad) / zoom, (x + clientWidth + pad) / zoom];
}

export const filterByTimeRange = (items, [start, end]) =>
  items.filter((n) => n.end >= start && n.start <= end);

export function shiftWordTexts(texts, [start, end], direction) {
  if ((direction > 0 && end >= texts.length - 1) || (direction < 0 && start <= 0)) {
    return texts;
  }
  const next = [...texts];
  if (direction > 0) {
    next.copyWithin(start + 1, start, -1);
    next[start] = "";
  } else {
    next.copyWithin(start - 1, start);
    next[next.length - 1] = "";
  }

  return next;
}

export function marqueeHitIds({ notes, x1, y1, x2, y2, keyboardWidth, zoom, rowHeight, maxMidi }) {
  const left = Math.min(x1, x2);
  const right = Math.max(x1, x2);
  const top = Math.min(y1, y2);
  const bottom = Math.max(y1, y2);
  return notes
    .filter((n) => {
      const x = keyboardWidth + n.start * zoom;
      const y = (maxMidi - n.note) * rowHeight;
      return (
        x + (n.end - n.start) * zoom >= left && x <= right && y + rowHeight >= top && y <= bottom
      );
    })
    .map((n) => n._id);
}

export const initialDocument = { notes: [], wordTexts: [], wordBounds: [], past: [], future: [] };
const snapshot = ({ notes, wordTexts, wordBounds }) => ({
  notes: cloneNotes(notes),
  wordTexts: [...wordTexts],
  wordBounds: cloneNotes(wordBounds)
});
const remember = (state) => [...state.past.slice(-79), snapshot(state)];

const reducers = {
  load: (_, a) => ({
    ...initialDocument,
    notes: normalizeNotes(a.notes),
    wordTexts: a.wordTexts || [],
    wordBounds: a.wordBounds || []
  }),
  edit: (s, a) => ({
    ...s,
    notes: normalizeNotes(a.notes),
    past: a.record ? remember(s) : s.past,
    future: a.record ? [] : s.future
  }),
  shiftWords: (s, a) => ({
    ...s,
    wordTexts: a.wordTexts,
    past: remember(s),
    future: []
  }),
  resizeWord: (s, a) => ({
    ...s,
    wordBounds: a.wordBounds,
    past: a.record ? remember(s) : s.past,
    future: a.record ? [] : s.future
  }),
  remember: (s, a) => ({
    ...s,
    past: [
      ...s.past.slice(-79),
      snapshot({
        notes: a.notes ?? s.notes,
        wordTexts: a.wordTexts ?? s.wordTexts,
        wordBounds: a.wordBounds ?? s.wordBounds
      })
    ],
    future: []
  }),
  undo: (s) => {
    const prev = s.past.at(-1);
    return prev
      ? { ...s, ...prev, past: s.past.slice(0, -1), future: [snapshot(s), ...s.future] }
      : s;
  },
  redo: (s) => {
    const next = s.future[0];
    return next ? { ...s, ...next, past: [...s.past, snapshot(s)], future: s.future.slice(1) } : s;
  }
};

export const documentReducer = (state, action) => reducers[action.type]?.(state, action) ?? state;

export const serializeNotes = (notes, words = []) =>
  notes.flatMap((n) =>
    words.flatMap((w, word_index) => {
      const start = roundTime(Math.max(n.start, +w.start));
      const end = roundTime(Math.min(n.end, +w.end));
      return end > start ? [{ note: n.note, start, end, word_index }] : [];
    })
  );

export const serializeWordBounds = (words) =>
  words.map(({ start, end }) => ({ start: roundTime(start), end: roundTime(end) }));
