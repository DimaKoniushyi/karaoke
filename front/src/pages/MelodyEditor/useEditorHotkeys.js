import { useEffect } from "react";
import { isEditableHotkeyTarget } from "../../utils/hotkeys";

// Global keyboard shortcuts for the note editor. A single window-level
// listener rather than per-control handlers, since most of these (undo,
// play/pause, arrow-key nudging) have no single element that owns focus.
export default function useEditorHotkeys({
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
}) {
  useEffect(() => {
    const hotkey = (event) => {
      // OS key-repeat would otherwise spam save()/undo() on every auto-repeat
      // tick while a key is held, and repeatedly flip play/pause under Space.
      if (event.repeat || event.isComposing || isEditableHotkeyTarget(event.target)) return;
      const modifier = event.ctrlKey || event.metaKey;
      const { code } = event;
      if (modifier && code === "KeyS") save();
      else if (modifier && code === "KeyZ") (event.shiftKey ? redo : undo)();
      else if (modifier && code === "KeyY") redo();
      else if (modifier && code === "KeyA") setSelected(notes.map(({ _id }) => _id));
      else if (modifier && code === "KeyC") copy();
      else if (modifier && code === "KeyX") {
        copy();
        remove();
      } else if (modifier && code === "KeyV") paste();
      else if (modifier && code === "KeyD") duplicate();
      else if (["Delete", "Backspace"].includes(event.key)) remove();
      else if (code === "Space")
        (transportRef.current.playing ? transportRef.current.pause : transportRef.current.play)?.();
      else if (event.key === "Home") transportRef.current.seek?.(0);
      else if (event.key === "End") transportRef.current.seek?.(duration);
      else if (event.altKey && selectedWords.length && event.key === "ArrowLeft") shiftWords(-1);
      else if (event.altKey && selectedWords.length && event.key === "ArrowRight") shiftWords(1);
      else if (event.key === "ArrowLeft")
        modifier ? nudge(event.shiftKey ? -0.25 : -0.05) : selectAdjacent(-1);
      else if (event.key === "ArrowRight")
        modifier ? nudge(event.shiftKey ? 0.25 : 0.05) : selectAdjacent(1);
      else if (event.key === "ArrowUp" && selected.length) nudge(0, event.shiftKey ? 12 : 1);
      else if (event.key === "ArrowDown" && selected.length) nudge(0, event.shiftKey ? -12 : -1);
      else if (event.key === "Escape") {
        setSelected([]);
        setSelectedWords([]);
      } else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", hotkey);
    return () => window.removeEventListener("keydown", hotkey);
  }, [
    copy,
    duplicate,
    duration,
    notes,
    nudge,
    paste,
    redo,
    remove,
    save,
    selectAdjacent,
    selected,
    selectedWords,
    setSelected,
    setSelectedWords,
    shiftWords,
    transportRef,
    undo
  ]);
}
