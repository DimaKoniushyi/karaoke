import { expect, test } from "vitest";
import {
  adjacentNoteId,
  canMergeSelectedNotes,
  canonicalLyricProjection,
  constrainedMoveDelta,
  deleteNotes,
  documentReducer,
  filterByTimeRange,
  initialDocument,
  marqueeHitIds,
  mergeSelectedNotes,
  normalizeNotes,
  notesOverlappingWords,
  recombineAdjacentEqualPitchNotes,
  resizeBounds,
  roundTime,
  serializeNotes,
  serializeWordBounds,
  shiftWordTexts,
  visibleTimeRange,
  wordResizeBounds
} from "../src/pages/MelodyEditor/model.js";

const note = (id, start, end, pitch = 60, word = 0) => ({
  _id: id,
  note: pitch,
  start,
  end,
  word_index: word,
  word_start: word * 2,
  word_end: word * 2 + 2
});

test("normalizes, clamps, sorts and rejects invalid or overlapping notes", () => {
  const result = normalizeNotes([
    note("late", 1.2, 1.8, 64),
    note("first", -1, 0.8),
    note("overlap", 0.5, 1.4, 62),
    { ...note("invalid", 0, 1), note: 128 },
    { ...note("empty", 1, 1), _id: "" }
  ]);
  expect(result.map(({ _id }) => _id)).toEqual(["first", "late"]);
  expect(result[0].start).toBe(0);
  expect(normalizeNotes([{ ...note("", 0, 1), _id: "" }])[0]._id).toContain("note-0");
  expect(normalizeNotes()).toEqual([]);
  expect(
    normalizeNotes([
      note("zero", 0, 0.2, 0),
      note("top", 0.2, 0.4, 127),
      note("negative", 0.4, 0.6, -1),
      note("fraction", 0.6, 0.8, 60.5),
      { ...note("bad-word", 0.8, 1), word_index: 0.5 }
    ]).map(({ _id }) => _id)
  ).toEqual(["zero", "top"]);
  expect(normalizeNotes([note("higher", 0, 0.2, 64), note("lower", 2, 2.2, 60, 1)]).map(({ _id }) => _id)).toEqual(["higher", "lower"]);
  expect(normalizeNotes([note("high", 0, 0.2, 72), note("low", 0.2, 0.4, 48)]).map(({ note: pitch }) => pitch)).toEqual([72, 48]);
  expect(normalizeNotes([note("higher-pitch", 0, 0.2, 72), note("lower-pitch", 0, 0.2, 48)]).map(({ _id }) => _id)).toEqual([
    "lower-pitch"
  ]);
});

test("merges adjacent notes -- including across a word boundary -- and deletes selected notes", () => {
  // "a" and "b" are in different words (word_index 0 and 9, with bounds that
  // only cover their own note); merging them is exactly the case this
  // exists for: a sustained note that got split where the lyric word
  // happened to end.
  const notes = [
    { ...note("a", 0, 0.5, 60, 0), word_start: 0, word_end: 0.5 },
    { ...note("b", 0.5, 1, 64, 9), word_start: 0.5, word_end: 1 },
    note("c", 2, 3, 70, 1)
  ];
  expect(canMergeSelectedNotes(notes, ["a", "b"])).toBe(true);
  const merged = mergeSelectedNotes(notes, ["a", "b"]);
  expect(merged.selectedId).toBe("a");
  expect(merged.notes.map(({ _id }) => _id)).toEqual(["a", "c"]);
  const result = merged.notes.find(({ _id }) => _id === "a");
  expect(result).toMatchObject({ start: 0, end: 1, note: 62 });
  // the merged note's own bounds now cover the union of both words, so a
  // later move/resize isn't clamped back down to just the first word
  expect(result.word_start).toBe(0);
  expect(result.word_end).toBe(1);
  // "a" and "c" are not adjacent -- "b" sits between them -- so merging them
  // while leaving "b" unselected is refused
  expect(mergeSelectedNotes(notes, ["a", "c"]).notes).toBe(notes);
  expect(canMergeSelectedNotes(notes, ["a", "c"])).toBe(false);
  expect(mergeSelectedNotes(notes, []).selectedId).toBeNull();
  expect(mergeSelectedNotes(notes, ["a"]).selectedId).toBe("a");
  expect(canMergeSelectedNotes(notes, ["a"])).toBe(false);
  expect(deleteNotes(notes, ["b"]).map(({ _id }) => _id)).toEqual(["a", "c"]);
});

test("finds adjacent notes in both directions and without an existing selection", () => {
  const notes = [note("b", 1, 1.5), note("a", 0, 0.5)];
  expect(adjacentNoteId(notes, [], 1)).toBe("a");
  expect(adjacentNoteId(notes, [], -1)).toBe("b");
  expect(adjacentNoteId(notes, ["a"], 1)).toBe("b");
  expect(adjacentNoteId(notes, ["b"], -1)).toBe("a");
  expect(adjacentNoteId(notes, ["a", "b"], 1)).toBe("b");
  expect(adjacentNoteId(notes, ["a", "b"], -1)).toBe("a");
  expect(adjacentNoteId(notes, ["b"], 1)).toBe("b");
  expect(adjacentNoteId(notes, ["a"], -1)).toBe("a");
  expect(adjacentNoteId(notes, ["a"], 0)).toBe("a");
  expect(adjacentNoteId([], [], 1)).toBeNull();
});

test("constrains movement and resizing to the word and neighbouring notes in that same word", () => {
  // notes in different words are allowed to sound at the same time (a word
  // can carry a harmony/backing note while the next word's own note has
  // already started), so collisions are only ever checked within one word
  const notes = [note("a", 0.2, 0.6), note("b", 0.8, 1.2)];
  expect(constrainedMoveDelta(notes, ["a"], -1)).toBeCloseTo(-0.2);
  expect(constrainedMoveDelta(notes, ["a"], 1)).toBeCloseTo(0.2);
  expect(constrainedMoveDelta(notes, [], 1)).toBe(0);
  expect(constrainedMoveDelta([note("a", 0.2, 0.5), note("b", 0.7, 1), note("other", 2, 3, 70, 1)], ["a", "b"], 2)).toBe(1);
  // "other" is in a different word and sits well within where "a" would
  // move to -- it does not block the move at all
  expect(constrainedMoveDelta([note("a", 0.2, 0.5), note("other", 0.8, 1.2, 70, 1)], ["a"], 2)).toBeCloseTo(1.5);
  expect(constrainedMoveDelta([note("previous", 0, 0.2), note("moving", 0.2, 0.5), note("next", 0.5, 0.8)], ["moving"], -1)).toBe(0);
  expect(constrainedMoveDelta([note("previous", 0, 0.2), note("moving", 0.2, 0.5), note("next", 0.5, 0.8)], ["moving"], 1)).toBe(0);
  expect(resizeBounds(notes, "a")).toEqual({
    minStart: 0,
    maxStart: 0.57,
    minEnd: 0.23,
    maxEnd: 0.8
  });
  expect(resizeBounds(notes, "missing")).toBeNull();
  expect(resizeBounds([note("previous", 0, 0.2), ...notes], "b").minStart).toBe(0.6);
  expect(resizeBounds([note("previous", 0, 0.2), note("moving", 0.2, 0.5), note("next", 0.5, 0.8)], "moving")).toMatchObject({
    minStart: 0.2,
    maxEnd: 0.5
  });
  // a note from a different word never constrains a resize, even when it
  // sits closer than the note's own word bound
  expect(resizeBounds([note("moving", 0.2, 0.5), note("other-word", 2, 3, 70, 1)], "moving")).toMatchObject({
    minStart: 0,
    maxEnd: 2
  });
  expect(resizeBounds([note("moving", 0.2, 0.5), note("closer-word", 0.7, 1, 70, 1)], "moving")).toMatchObject({
    minStart: 0,
    maxEnd: 2
  });
});

test("finds notes overlapping the selected words by time, not by their stored word_index", () => {
  const words = [
    { index: 0, text: "one", start: 0, end: 1 },
    { index: 1, text: "two", start: 1, end: 2 },
    { index: 2, text: "three", start: 2, end: 3 }
  ];
  const notes = [
    note("inside-one", 0.1, 0.5, 60, 5),
    // spans words 1 and 2, but its own word_index only names one of them --
    // overlap must still find it for word 2
    note("spans-two-and-three", 1.5, 2.5, 62, 1),
    note("far-away", 2.9, 3.0, 64, 2)
  ];
  expect(notesOverlappingWords(notes, words, [0])).toEqual(new Set(["inside-one"]));
  expect(notesOverlappingWords(notes, words, [2])).toEqual(
    new Set(["spans-two-and-three", "far-away"])
  );
  expect(notesOverlappingWords(notes, words, [])).toEqual(new Set());
  expect(notesOverlappingWords(notes, words, [99])).toEqual(new Set());
});

test("visibleTimeRange pads the scrolled viewport with overscan on both sides", () => {
  const [start, end] = visibleTimeRange(
    { scrollLeft: 1000, clientWidth: 800 },
    { keyboardWidth: 100, zoom: 100 }
  );
  expect(start).toBeCloseTo(4.2, 5);
  expect(end).toBeCloseTo(21.8, 5);
});

test("filterByTimeRange keeps only items overlapping the range, including partial overlaps", () => {
  const items = [
    note("before", 0, 4.1),
    note("partially-before", 3, 4.5),
    note("inside", 5, 10),
    note("partially-after", 21, 22),
    note("after", 22, 23)
  ];
  expect(filterByTimeRange(items, [4.2, 21.8]).map((item) => item._id)).toEqual([
    "partially-before",
    "inside",
    "partially-after"
  ]);
});

test("projects canonical lyrics and detects marquee intersections", () => {
  expect(canonicalLyricProjection([{ text: "Я", start: "1", end: "2" }])).toEqual([{ index: 0, text: "Я", start: 1, end: 2 }]);
  expect(canonicalLyricProjection()).toEqual([]);
  expect(canonicalLyricProjection([{ start: 0, end: 1 }])[0].text).toBe("");
  expect(
    marqueeHitIds({
      notes: [note("hit", 0, 1, 60), note("miss", 2, 3, 72, 1)],
      x1: 80,
      y1: 10,
      x2: 200,
      y2: 40,
      keyboardWidth: 80,
      zoom: 100,
      rowHeight: 10,
      maxMidi: 62
    })
  ).toEqual(["hit"]);
  const bounds = {
    notes: [note("edge", 0, 1, 60)],
    keyboardWidth: 80,
    zoom: 100,
    rowHeight: 10,
    maxMidi: 62
  };
  expect(marqueeHitIds({ ...bounds, x1: 180, x2: 180, y1: 20, y2: 30 })).toEqual(["edge"]);
  expect(marqueeHitIds({ ...bounds, x1: 181, x2: 200, y1: 31, y2: 40 })).toEqual([]);
  expect(marqueeHitIds({ ...bounds, x1: 0, x2: 79, y1: 20, y2: 30 })).toEqual([]);
  expect(marqueeHitIds({ ...bounds, x1: 80, x2: 180, y1: 0, y2: 19 })).toEqual([]);
  expect(marqueeHitIds({ ...bounds, x1: 80, x2: 180, y1: 31, y2: 40 })).toEqual([]);
});

test("document history records commits and previews without losing redo", () => {
  const first = [note("a", 0, 1)];
  const second = [note("a", 0, 1, 62)];
  let state = documentReducer(initialDocument, { type: "load", notes: first });
  state = documentReducer(state, { type: "edit", notes: second, record: true });
  expect(state.past).toHaveLength(1);
  state = documentReducer(state, { type: "undo" });
  expect(state.notes[0].note).toBe(60);
  expect(state.future).toHaveLength(1);
  state = documentReducer(state, { type: "redo" });
  expect(state.notes[0].note).toBe(62);
  const preview = documentReducer(state, { type: "edit", notes: first, record: false });
  expect(preview.past).toEqual(state.past);
  const remembered = documentReducer(preview, { type: "remember", notes: second });
  expect(remembered.past.at(-1).notes[0].note).toBe(62);
  expect(documentReducer(initialDocument, { type: "undo" })).toBe(initialDocument);
  expect(documentReducer(initialDocument, { type: "redo" })).toBe(initialDocument);
  const dirty = { notes: first, past: [second], future: [second] };
  expect(documentReducer(dirty, { type: "load", notes: first })).toMatchObject({
    past: [],
    future: []
  });
  let capped = initialDocument;
  for (let index = 0; index < 82; index += 1)
    capped = documentReducer(capped, {
      type: "edit",
      notes: [note(String(index), 0, 1)],
      record: true
    });
  expect(capped.past).toHaveLength(80);
  const undone = documentReducer(capped, { type: "undo" });
  expect(undone.past).toHaveLength(79);
  const redone = documentReducer(undone, { type: "redo" });
  expect(redone.future).toHaveLength(0);
  expect(redone.past).toHaveLength(80);
});

test("word text shifts share the same undo/redo history as note edits", () => {
  const notes = [note("a", 0, 1)];
  let state = documentReducer(initialDocument, {
    type: "load",
    notes,
    wordTexts: ["one", "two", "three"]
  });
  state = documentReducer(state, {
    type: "shiftWords",
    wordTexts: shiftWordTexts(state.wordTexts, [0, 0], 1)
  });
  expect(state.wordTexts).toEqual(["two", "one", "three"]);
  expect(state.past).toHaveLength(1);
  const undone = documentReducer(state, { type: "undo" });
  expect(undone.wordTexts).toEqual(["one", "two", "three"]);
  expect(undone.notes[0].note).toBe(60);
  const redone = documentReducer(undone, { type: "redo" });
  expect(redone.wordTexts).toEqual(["two", "one", "three"]);
});

test("shifts a selected run of word texts forward or backward, swapping places with its one adjacent neighbour", () => {
  const texts = ["a", "b", "c", "d", "e"];
  // moving [1,2] forward swaps the run with its right neighbour (d): the
  // run slides one slot right, and d takes the vacated slot -- no word is
  // ever dropped or blanked.
  expect(shiftWordTexts(texts, [1, 2], 1)).toEqual(["a", "d", "b", "c", "e"]);
  // moving [2,3] backward swaps the run with its left neighbour (b).
  expect(shiftWordTexts(texts, [2, 3], -1)).toEqual(["a", "c", "d", "b", "e"]);
  // a single word behaves the same as a run of length one
  expect(shiftWordTexts(texts, [0, 0], 1)).toEqual(["b", "a", "c", "d", "e"]);
  expect(shiftWordTexts(texts, [1, 1], -1)).toEqual(["b", "a", "c", "d", "e"]);
  // at either edge, with nowhere to move into, the array is returned unchanged
  expect(shiftWordTexts(texts, [0, 1], -1)).toBe(texts);
  expect(shiftWordTexts(texts, [3, 4], 1)).toBe(texts);
});

test("serializes notes by clipping them to every word they overlap, splitting a merged cross-word note back apart", () => {
  const words = [
    { start: 0, end: 1 },
    { start: 1, end: 2 }
  ];
  expect(serializeNotes([{ ...note("a", 0, 1), extra: true }], words)).toEqual([
    { note: 60, start: 0, end: 1, word_index: 0 }
  ]);
  // a note spanning both words (e.g. the result of merging across the word
  // boundary) is split back into one clipped piece per word it overlaps --
  // the backend's per-word note contract never has to change
  expect(serializeNotes([note("merged", 0.5, 1.5)], words)).toEqual([
    { note: 60, start: 0.5, end: 1, word_index: 0 },
    { note: 60, start: 1, end: 1.5, word_index: 1 }
  ]);
  expect(serializeNotes([note("a", 0, 1)], [])).toEqual([]);
  expect(roundTime(1.23456)).toBe(1.235);
});

test("recombines touching equal-pitch pieces on load, so a merge survives a save + reload round trip", () => {
  // this is exactly what serializeNotes produces for a note that spans two
  // words: two touching, equal-pitch pieces, one clipped to each word
  const split = [
    note("piece-1", 0.5, 1, 62, 0),
    note("piece-2", 1, 1.5, 62, 1),
    note("elsewhere", 3, 3.5, 64, 2)
  ];
  const recombined = recombineAdjacentEqualPitchNotes(split);
  expect(recombined).toHaveLength(2);
  expect(recombined[0]).toMatchObject({
    note: 62,
    start: 0.5,
    end: 1.5,
    word_start: 0,
    word_end: 4
  });
  expect(recombined[1]).toMatchObject({ note: 64, start: 3, end: 3.5 });
  // different pitch, or a real gap between them, must not be glued together
  expect(
    recombineAdjacentEqualPitchNotes([note("a", 0, 0.5, 60), note("b", 0.5, 1, 64)])
  ).toHaveLength(2);
  expect(
    recombineAdjacentEqualPitchNotes([note("a", 0, 0.5, 60), note("b", 0.6, 1, 60)])
  ).toHaveLength(2);
});

test("bounds a word's resize to its neighbours' starts, since only word starts must stay ordered", () => {
  const bounds = [{ start: 0, end: 1 }, { start: 1, end: 2 }, { start: 2, end: 3 }];
  // the middle word's left edge can't pass the previous word's start, and
  // can't pass the next word's start either (that would make the next word
  // start earlier than this one, which lyricsSync forbids)
  expect(wordResizeBounds(bounds, 1, 10)).toEqual({
    minStart: 0,
    maxStart: 1.95,
    minEnd: 1.05,
    maxEnd: 10
  });
  // a word's end has no neighbour constraint at all -- words are allowed to
  // overlap in time -- so it is only bounded by the song's duration
  expect(wordResizeBounds(bounds, 0, 10).maxEnd).toBe(10);
  expect(wordResizeBounds(bounds, 5, 10)).toBeNull();
  expect(serializeWordBounds([{ start: 1.23456, end: 2.98765 }])).toEqual([
    { start: 1.235, end: 2.988 }
  ]);
});

test("word text shifts and word resizes share the same undo/redo history as note edits", () => {
  const notes = [note("a", 0, 1)];
  const wordBounds = [{ start: 0, end: 1 }, { start: 1, end: 2 }];
  let state = documentReducer(initialDocument, {
    type: "load",
    notes,
    wordTexts: ["one", "two"],
    wordBounds
  });
  state = documentReducer(state, {
    type: "resizeWord",
    wordBounds: [{ start: 0, end: 1.5 }, { start: 1, end: 2 }],
    record: true
  });
  expect(state.wordBounds[0].end).toBe(1.5);
  expect(state.past).toHaveLength(1);
  const undone = documentReducer(state, { type: "undo" });
  expect(undone.wordBounds[0].end).toBe(1);
  const redone = documentReducer(undone, { type: "redo" });
  expect(redone.wordBounds[0].end).toBe(1.5);
});
