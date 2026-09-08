import { clamp } from "../../../../utils/math";

export const PIANO_ROLL_VIEW = {
  width: 1200,
  height: 288,
  keyboardRatio: 86 / 1200,
  seconds: 10,
  lead: 2.5
};

const finite = (value, fallback) => {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
};

export function normalizePianoNotes(notes = [], shift = 0) {
  const result = [];
  const offset = finite(shift, 0);

  notes.forEach((source, index) => {
    if (!source) return;

    const note = finite(source.note, NaN) + offset;
    const start = finite(source.start, NaN);
    const end = finite(source.end, NaN);
    if (![note, start, end].every(Number.isFinite) || end <= start) return;

    result.push({
      ...source,
      key: source._id || `${start}-${end}-${note}-${index}`,
      note,
      start,
      end
    });
  });

  return result.sort((a, b) => a.start - b.start || a.note - b.note);
}

export function pianoPitchRange(notes = [], sungMidi) {
  let min = Infinity;
  let max = -Infinity;

  for (const item of notes) {
    const note = item?.note;
    if (!Number.isFinite(note)) continue;
    min = Math.min(min, note);
    max = Math.max(max, note);
  }

  if (!Number.isFinite(min)) min = max = Number.isFinite(sungMidi) ? sungMidi : 60;

  return {
    min: clamp(Math.floor(min) - 2, 0, 127),
    max: clamp(Math.ceil(max) + 2, 0, 127)
  };
}

// Shared by both piano-roll renderers (Canvas2D fallback and Pixi/WebGL) so
// the same numbers drive whichever backend is active instead of two
// independently hand-copied sets of magic constants.
export const noteHeight = (rowHeight) => clamp(rowHeight * 0.72, 5, 15);
export const connectionLineWidth = (rowHeight) => clamp(rowHeight * 0.24, 2, 5);
export const pitchDotRadius = (rowHeight) => Math.max(3, rowHeight * 0.2);
export const noteOpacity = (note, time) =>
  note.state === "past" ? clamp(0.58 * (1 - (time - note.end) / 2.8), 0.08, 0.58) : 1;

export function pianoRollFrame(notes = [], currentTime = 0, size, range) {
  const view = size || PIANO_ROLL_VIEW;
  const pitch = range || pianoPitchRange(notes);
  const time = finite(currentTime, 0);
  const start = Math.max(0, time - PIANO_ROLL_VIEW.lead);
  const end = start + PIANO_ROLL_VIEW.seconds;
  const min = finite(pitch.min, 0);
  const max = Math.max(min, finite(pitch.max, 127));
  const width = Math.max(1, finite(view.width, PIANO_ROLL_VIEW.width));
  const height = Math.max(1, finite(view.height, PIANO_ROLL_VIEW.height));
  const keyboard = width * PIANO_ROLL_VIEW.keyboardRatio;
  const lane = width - keyboard;
  const rowHeight = height / (max - min + 1);
  const x = (value) =>
    keyboard + ((finite(value, start) - start) / PIANO_ROLL_VIEW.seconds) * lane;
  const y = (note) => height - (finite(note, min) - min + 1) * rowHeight;

  const visible = notes
    .filter((note) => note?.end >= start && note.start <= end)
    .map((note) => ({
      ...note,
      left: Math.max(keyboard, x(note.start)),
      right: Math.min(width, x(note.end)),
      state: time < note.start ? "future" : time < note.end ? "current" : "past"
    }));

  const connections = [];
  for (let i = 1; i < visible.length; i++) {
    const previous = visible[i - 1];
    const note = visible[i];
    const gap = note.start - previous.end;
    if (gap > 0.04) continue;

    const touching = gap <= 0;
    connections.push({
      fromX: touching ? note.left : previous.right,
      fromY: y(previous.note) + rowHeight / 2,
      toX: touching ? note.left + clamp(previous.right - note.left, 4, 18) : note.left,
      toY: y(note.note) + rowHeight / 2,
      state: note.state
    });
  }

  return {
    start,
    end,
    time,
    width,
    height,
    keyboard,
    lane,
    rowHeight,
    min,
    max,
    playhead: x(time),
    x,
    y,
    connections,
    notes: visible
  };
}
