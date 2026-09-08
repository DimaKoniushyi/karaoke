import { useCallback, useRef } from "react";
import { clamp } from "../../utils/math";
import { cloneNotes, roundTime } from "./model";

const selectedNotes = (notes, ids) => {
  const selected = new Set(ids);
  return notes.filter(({ _id }) => selected.has(_id));
};

// Copy/paste/duplicate for the selected notes. `insertCopies` is the shared
// primitive both paste and duplicate build on: it re-times a cloned note
// list so its earliest note lands at `anchor`, then commits and selects it.
export default function useEditorClipboard({
  notes,
  selected,
  setSelected,
  edit,
  duration,
  transportRef
}) {
  const clipboardRef = useRef([]);

  const insertCopies = useCallback(
    (sources, anchor, prefix) => {
      if (!sources.length) return;
      const first = Math.min(...sources.map(({ start }) => start));
      const stamp = Date.now();
      const copies = sources.map((note, index) => {
        const offset = anchor - first;
        return {
          ...note,
          _id: `${prefix}-${stamp}-${index}`,
          start: roundTime(note.start + offset),
          end: roundTime(note.end + offset),
          word_start: roundTime(note.word_start + offset),
          word_end: roundTime(note.word_end + offset)
        };
      });
      edit([...notes, ...copies]);
      setSelected(copies.map(({ _id }) => _id));
    },
    [edit, notes, setSelected]
  );

  const copy = useCallback(() => {
    clipboardRef.current = cloneNotes(selectedNotes(notes, selected));
  }, [notes, selected]);

  const paste = useCallback(() => {
    const copied = clipboardRef.current;
    if (!copied.length) return;
    const span =
      Math.max(...copied.map(({ end }) => end)) - Math.min(...copied.map(({ start }) => start));
    insertCopies(
      copied,
      clamp(transportRef.current.timeRef?.current || 0, 0, Math.max(0, duration - span)),
      "paste"
    );
  }, [duration, insertCopies, transportRef]);

  const duplicate = useCallback(() => {
    const chosen = selectedNotes(notes, selected);
    if (!chosen.length) return;
    const first = Math.min(...chosen.map(({ start }) => start));
    const last = Math.max(...chosen.map(({ end }) => end));
    const offset = Math.max(0.03, last - first);
    if (last + offset > duration) return;
    insertCopies(chosen, first + offset, "duplicate");
  }, [duration, insertCopies, notes, selected]);

  return { copy, paste, duplicate };
}
