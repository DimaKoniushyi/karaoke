import { useCallback, useRef, useState } from "react";
import { adjacentNoteId } from "./model";

// Note and word selection: which ids are selected, plus the anchor a
// shift-click word range extends from. Dragging/marquee selection (which
// also writes into `selected`) lives in useNoteDragging; this hook only
// owns the selection state itself and the two selection-only gestures.
export default function useNoteSelection({ notes, transportRef }) {
  const [selected, setSelected] = useState([]);
  const [selectedWords, setSelectedWords] = useState([]);
  const wordSelectAnchor = useRef(null);

  const selectWord = useCallback((index, event) => {
    if (event?.shiftKey && wordSelectAnchor.current !== null) {
      const anchor = wordSelectAnchor.current;
      const lo = Math.min(anchor, index);
      const hi = Math.max(anchor, index);
      setSelectedWords(Array.from({ length: hi - lo + 1 }, (_, offset) => lo + offset));
      return;
    }
    wordSelectAnchor.current = index;
    setSelectedWords([index]);
  }, []);

  const selectAdjacent = useCallback(
    (direction) => {
      const id = adjacentNoteId(notes, selected, direction);
      if (!id) return;
      const note = notes.find(({ _id }) => _id === id);
      setSelected([id]);
      transportRef.current.seek?.(note.start);
      transportRef.current.tone?.(note.note);
    },
    [notes, selected, transportRef]
  );

  return {
    selected,
    setSelected,
    selectedWords,
    setSelectedWords,
    wordSelectAnchor,
    selectWord,
    selectAdjacent
  };
}
