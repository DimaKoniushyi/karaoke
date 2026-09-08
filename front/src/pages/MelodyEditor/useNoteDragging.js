import { useCallback, useRef, useState } from "react";
import { clamp } from "../../utils/math";
import {
  cloneNotes,
  constrainedMoveDelta,
  marqueeHitIds,
  resizeBounds,
  roundTime,
  wordResizeBounds
} from "./model";

// Pointer state machine for dragging/resizing notes and words, and for the
// marquee (rubber-band) selection box. Three refs, one per gesture kind,
// since only one gesture is ever active at a time but each needs its own
// snapshot of where it started.
export default function useNoteDragging({
  notes,
  edit,
  dispatch,
  selected,
  setSelected,
  zoom,
  rowHeight,
  duration,
  keyboardWidth,
  maxMidi,
  wordBounds,
  transportRef,
  surfaceRef
}) {
  const [selectionBox, setSelectionBox] = useState(null);
  const dragRef = useRef(null);
  const wordDragRef = useRef(null);
  const marqueeRef = useRef(null);

  const startWordResize = useCallback(
    (event, index, side) => {
      event.preventDefault();
      event.stopPropagation();
      wordDragRef.current = {
        index,
        side,
        x: event.clientX,
        snapshot: wordBounds.map((bound) => ({ ...bound })),
        moved: false
      };
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [wordBounds]
  );

  const startDrag = useCallback(
    (event, note, mode = "move") => {
      event.preventDefault();
      event.stopPropagation();
      const additive = event.shiftKey || event.ctrlKey || event.metaKey;
      const ids = selected.includes(note._id)
        ? selected
        : additive
          ? [...selected, note._id]
          : [note._id];
      setSelected(ids);
      dragRef.current = {
        id: note._id,
        ids,
        mode,
        x: event.clientX,
        y: event.clientY,
        snapshot: cloneNotes(notes),
        originals: new Map(
          notes.filter(({ _id }) => ids.includes(_id)).map((item) => [item._id, { ...item }])
        ),
        moved: false
      };
      transportRef.current.tone?.(note.note);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [notes, selected, setSelected, transportRef]
  );

  const movePointer = useCallback(
    (event) => {
      const wordDrag = wordDragRef.current;
      if (wordDrag) {
        const dx = (event.clientX - wordDrag.x) / zoom;
        wordDrag.moved ||= Math.abs(event.clientX - wordDrag.x) > 1;
        const original = wordDrag.snapshot[wordDrag.index];
        const limits = wordResizeBounds(wordDrag.snapshot, wordDrag.index, duration);
        dispatch({
          type: "resizeWord",
          wordBounds: wordDrag.snapshot.map((bound, index) => {
            if (index !== wordDrag.index) return bound;
            if (wordDrag.side === "left") {
              const start = clamp(original.start + dx, limits.minStart, limits.maxStart);
              return { ...bound, start: roundTime(start) };
            }
            const end = clamp(original.end + dx, limits.minEnd, limits.maxEnd);
            return { ...bound, end: roundTime(end) };
          }),
          record: false
        });
        return;
      }
      const drag = dragRef.current;
      if (drag) {
        const dx = (event.clientX - drag.x) / zoom;
        const dy = event.shiftKey ? 0 : Math.round((drag.y - event.clientY) / rowHeight);
        drag.moved ||= Math.abs(event.clientX - drag.x) > 1 || Math.abs(event.clientY - drag.y) > 1;
        if (drag.mode === "move") {
          const safe = constrainedMoveDelta(drag.snapshot, drag.ids, dx);
          edit(
            notes.map((note) => {
              const original = drag.originals.get(note._id);
              return original
                ? {
                    ...note,
                    start: roundTime(original.start + safe),
                    end: roundTime(original.end + safe),
                    note: clamp(original.note + dy, 0, 127)
                  }
                : note;
            }),
            false
          );
        } else {
          const original = drag.originals.get(drag.id);
          const bounds = resizeBounds(drag.snapshot, drag.id);
          const changed =
            drag.mode === "left"
              ? { start: roundTime(clamp(original.start + dx, bounds.minStart, bounds.maxStart)) }
              : { end: roundTime(clamp(original.end + dx, bounds.minEnd, bounds.maxEnd)) };
          edit(
            notes.map((note) => (note._id === drag.id ? { ...note, ...changed } : note)),
            false
          );
        }
        return;
      }
      const marquee = marqueeRef.current;
      if (!marquee || marquee.pointerId !== event.pointerId) return;
      const rect = surfaceRef.current.getBoundingClientRect();
      marquee.x2 = event.clientX - rect.left;
      marquee.y2 = event.clientY - rect.top;
      setSelectionBox({ ...marquee });
      const hits = marqueeHitIds({
        notes,
        ...marquee,
        keyboardWidth,
        zoom,
        rowHeight,
        maxMidi
      });
      setSelected([...new Set([...marquee.base, ...hits])]);
    },
    [
      dispatch,
      duration,
      edit,
      keyboardWidth,
      maxMidi,
      notes,
      rowHeight,
      setSelected,
      surfaceRef,
      zoom
    ]
  );

  const endPointer = useCallback(() => {
    const wordDrag = wordDragRef.current;
    if (wordDrag?.moved) dispatch({ type: "remember", wordBounds: wordDrag.snapshot });
    wordDragRef.current = null;
    const drag = dragRef.current;
    if (drag?.moved) dispatch({ type: "remember", notes: drag.snapshot });
    dragRef.current = null;
    marqueeRef.current = null;
    setSelectionBox(null);
  }, [dispatch]);

  const startMarquee = useCallback(
    (event) => {
      if (
        event.button !== 0 ||
        event.target.closest?.('[data-role="editor-note"], [data-role="editor-word"]')
      )
        return;
      const rect = surfaceRef.current.getBoundingClientRect();
      const x = event.clientX - rect.left;
      if (x < keyboardWidth) return;
      const additive = event.shiftKey || event.ctrlKey || event.metaKey;
      const value = {
        pointerId: event.pointerId,
        x1: x,
        y1: event.clientY - rect.top,
        x2: x,
        y2: event.clientY - rect.top,
        base: additive ? selected : []
      };
      marqueeRef.current = value;
      setSelectionBox(value);
      if (!additive) setSelected([]);
      event.currentTarget.setPointerCapture?.(event.pointerId);
    },
    [keyboardWidth, selected, setSelected, surfaceRef]
  );

  return { selectionBox, startDrag, movePointer, endPointer, startMarquee, startWordResize };
}
