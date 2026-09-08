import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { clamp } from "../../utils/math";
import { readJsonStorage } from "../../utils/storage";
import { persistUiPreferences } from "../../utils/ui-preferences";
import {
  canMergeSelectedNotes,
  computeEditorLayout,
  constrainedMoveDelta,
  deleteNotes,
  mergeSelectedNotes,
  roundTime,
  shiftWordTexts
} from "./model";
import useEditorClipboard from "./useEditorClipboard";
import useEditorHotkeys from "./useEditorHotkeys";
import useNoteDragging from "./useNoteDragging";
import useNoteSelection from "./useNoteSelection";

const STORAGE_KEY = "karaoke-melody-editor";
const preference = (value, fallback, minimum, maximum) => {
  const number = Number(value);
  return Number.isFinite(number) && number >= minimum && number <= maximum ? number : fallback;
};

export default function useEditorController({ document, dispatch, payload, save, transportRef }) {
  const saved = useMemo(() => readJsonStorage(STORAGE_KEY), []);
  const [zoom, setZoom] = useState(() => preference(saved.zoom, 48, 36, 600));
  const [rowHeight, setRowHeight] = useState(() => preference(saved.verticalZoom, 16, 10, 36));
  const [autoScroll, setAutoScroll] = useState(() => saved.autoScroll ?? true);
  const [playbackRate, setPlaybackRate] = useState(() => Number(saved.playbackRate) || 1);
  const [volumes, setVolumes] = useState(() => ({
    vocals: Number(saved.volumes?.vocals ?? 0.7),
    melody: Number(saved.volumes?.melody ?? 0.9),
    instrumental: Number(saved.volumes?.instrumental ?? 0.45)
  }));
  const shellRef = useRef(null);
  const surfaceRef = useRef(null);
  const playheadRef = useRef(null);
  const { notes, wordBounds, wordTexts } = document;
  const lyricsSync = payload?.lyrics_sync || {};
  const duration = Number(lyricsSync.duration) || Math.max(1, ...notes.map(({ end }) => end));
  const { minMidi, maxMidi, keyboardWidth, laneHeight, laneWidth } = useMemo(
    () => computeEditorLayout(notes, rowHeight, zoom, duration),
    [notes, rowHeight, zoom, duration]
  );
  const words = useMemo(
    () =>
      wordBounds.map((bound, index) => ({
        index,
        text: wordTexts[index] ?? "",
        start: bound.start,
        end: bound.end
      })),
    [wordBounds, wordTexts]
  );

  useEffect(() => {
    persistUiPreferences(api, "melody_editor", {
      autoScroll,
      playbackRate,
      verticalZoom: rowHeight,
      volumes,
      zoom
    });
  }, [autoScroll, playbackRate, rowHeight, volumes, zoom]);

  const edit = useCallback(
    (next, record = true) =>
      dispatch({ type: "edit", notes: typeof next === "function" ? next(notes) : next, record }),
    [dispatch, notes]
  );
  const undo = useCallback(() => dispatch({ type: "undo" }), [dispatch]);
  const redo = useCallback(() => dispatch({ type: "redo" }), [dispatch]);

  const {
    selected,
    setSelected,
    selectedWords,
    setSelectedWords,
    wordSelectAnchor,
    selectWord,
    selectAdjacent
  } = useNoteSelection({ notes, transportRef });

  const remove = useCallback(() => {
    if (!selected.length) return;
    edit(deleteNotes(notes, selected));
    setSelected([]);
  }, [edit, notes, selected, setSelected]);
  const merge = useCallback(() => {
    const result = mergeSelectedNotes(notes, selected);
    if (result.notes === notes) return;
    edit(result.notes);
    setSelected([result.selectedId]);
  }, [edit, notes, selected, setSelected]);
  const nudge = useCallback(
    (seconds, pitch = 0) => {
      if (!selected.length) return;
      const safe = constrainedMoveDelta(notes, selected, seconds);
      edit(
        notes.map((note) =>
          selected.includes(note._id)
            ? {
                ...note,
                start: roundTime(note.start + safe),
                end: roundTime(note.end + safe),
                note: clamp(note.note + pitch, 0, 127)
              }
            : note
        )
      );
    },
    [edit, notes, selected]
  );

  const { copy, paste, duplicate } = useEditorClipboard({
    notes,
    selected,
    setSelected,
    edit,
    duration,
    transportRef
  });

  const { selectionBox, startDrag, movePointer, endPointer, startMarquee, startWordResize } =
    useNoteDragging({
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
    });

  const shiftWords = useCallback(
    (direction) => {
      if (!wordTexts.length || !selectedWords.length) return;
      const start = Math.min(...selectedWords);
      const end = Math.max(...selectedWords);
      const next = shiftWordTexts(wordTexts, [start, end], direction);
      if (next === wordTexts) return;
      dispatch({ type: "shiftWords", wordTexts: next });
      const moved = start + direction;
      const movedEnd = end + direction;
      setSelectedWords(Array.from({ length: movedEnd - moved + 1 }, (_, offset) => moved + offset));
      wordSelectAnchor.current = moved;
    },
    [dispatch, selectedWords, setSelectedWords, wordSelectAnchor, wordTexts]
  );

  useEditorHotkeys({
    save,
    redo,
    undo,
    notes,
    copy,
    remove,
    paste,
    duplicate,
    transportRef,
    duration,
    selected,
    setSelected,
    selectedWords,
    setSelectedWords,
    shiftWords,
    selectAdjacent,
    nudge
  });

  return {
    autoScroll,
    canMerge: canMergeSelectedNotes(notes, selected),
    duration,
    endPointer,
    keyboardWidth,
    laneHeight,
    laneWidth,
    maxMidi,
    merge,
    minMidi,
    movePointer,
    notes,
    playbackRate,
    playheadRef,
    redo,
    remove,
    rowHeight,
    selected,
    selectionBox,
    selectWord,
    selectedWords,
    setAutoScroll,
    setPlaybackRate,
    setRowHeight,
    setSelected,
    setVolumes,
    setZoom,
    shellRef,
    shiftWords,
    startDrag,
    startMarquee,
    startWordResize,
    surfaceRef,
    undo,
    volumes,
    words,
    zoom
  };
}
