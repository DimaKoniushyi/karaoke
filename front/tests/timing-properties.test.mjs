import fc from "fast-check";
import { describe, expect, test } from "vitest";
import { normalizeNotes } from "../src/pages/MelodyEditor/model.js";
import { lyricsNoteFillPercent, mergeAdjacentLyricsNotes } from "../src/utils/lyrics-sync.js";
import { prepareEditorNotes } from "../src/pages/MelodyEditor/workers/editor-computation.js";

const finiteTime = fc.double({ min: 0, max: 600, noNaN: true, noDefaultInfinity: true });
const duration = fc.double({ min: 0.001, max: 20, noNaN: true, noDefaultInfinity: true });
const noteArbitrary = fc
  .tuple(finiteTime, duration, finiteTime, duration, fc.integer({ min: -12, max: 140 }), fc.nat(40), fc.uuid())
  .map(([wordStart, wordDuration, offset, noteDuration, note, wordIndex, id]) => ({
    _id: id,
    note,
    start: wordStart - 2 + (offset % (wordDuration + 4)),
    end: wordStart - 2 + (offset % (wordDuration + 4)) + noteDuration,
    word_index: wordIndex,
    word_start: wordStart,
    word_end: wordStart + wordDuration
  }));

describe("timing invariants", () => {
  test("editor worker computation keeps lyricsSync as the only note source", () => {
    const lyricsSync = {
      words: [
        {
          start: 1,
          end: 2,
          notes: [
            { start: 1.1, end: 1.4, note: 60 },
            { start: 1.4, end: 1.9, note: 62 }
          ]
        }
      ]
    };
    expect(
      prepareEditorNotes(lyricsSync).map(({ note, start, end, word_index, word_start, word_end }) => ({
        note,
        start,
        end,
        word_index,
        word_start,
        word_end
      }))
    ).toEqual([
      { note: 60, start: 1.1, end: 1.4, word_index: 0, word_start: 1, word_end: 2 },
      { note: 62, start: 1.4, end: 1.9, word_index: 0, word_start: 1, word_end: 2 }
    ]);
  });
  test("normalized melody notes always stay canonical and non-overlapping", () => {
    fc.assert(
      fc.property(fc.array(noteArbitrary, { maxLength: 160 }), (source) => {
        const notes = normalizeNotes(source);
        const endByWord = new Map();
        for (const current of notes) {
          expect(Number.isInteger(current.note)).toBe(true);
          expect(current.note).toBeGreaterThanOrEqual(0);
          expect(current.note).toBeLessThanOrEqual(127);
          expect(current.start).toBeGreaterThanOrEqual(current.word_start);
          expect(current.end).toBeLessThanOrEqual(current.word_end);
          expect(current.end).toBeGreaterThan(current.start);
          expect(current.start).toBeGreaterThanOrEqual(endByWord.get(current.word_index) ?? current.word_start);
          endByWord.set(current.word_index, current.end);
        }
      }),
      { numRuns: 250 }
    );
  });

  test("note-driven lyric fill is bounded and adjacent notes remain inside the word", () => {
    fc.assert(
      fc.property(
        finiteTime,
        duration,
        fc.array(fc.record({ note: fc.integer({ min: 0, max: 127 }), start: finiteTime, end: finiteTime }), { maxLength: 80 }),
        finiteTime,
        (start, length, notes, now) => {
          const word = { start, end: start + length, notes };
          for (const current of mergeAdjacentLyricsNotes(word)) {
            expect(current.start).toBeGreaterThanOrEqual(word.start);
            expect(current.end).toBeLessThanOrEqual(word.end);
            expect(current.end).toBeGreaterThan(current.start);
          }
          const fill = lyricsNoteFillPercent(word, now);
          expect(fill).toBeGreaterThanOrEqual(0);
          expect(fill).toBeLessThanOrEqual(100);
        }
      ),
      { numRuns: 250 }
    );
  });
});
