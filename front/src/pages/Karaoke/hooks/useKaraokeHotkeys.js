import { useEffect } from "react";
import useLatestRef from "../../../hooks/useLatestRef";
import { isHotkeyScopeActive, shouldIgnoreHotkey } from "../../../utils/hotkeys";

const SEEK_DELTA = { "seek-backward": -5, "seek-forward": 5 };

// Pure key -> command classification, kept separate from useKaraokeHotkeys
// so it can be unit-tested without mounting a component or touching the DOM.
export function getKaraokeHotkeyAction(event, scope) {
  if (
    event?.code === "Space" &&
    !event.defaultPrevented &&
    !event.isComposing &&
    !event.repeat &&
    isHotkeyScopeActive(scope)
  ) {
    return "toggle-playback";
  }
  if (shouldIgnoreHotkey(event, scope)) return null;
  if (event.code === "Escape") return "stop";
  if (event.code === "ArrowLeft") return "seek-backward";
  if (event.code === "ArrowRight") return "seek-forward";
  return null;
}

export default function useKaraokeHotkeys(
  scopeRef,
  { currentTime, duration, onTogglePlay, onSeek, onStop }
) {
  const handlers = useLatestRef({ currentTime, duration, onTogglePlay, onSeek, onStop });

  useEffect(() => {
    const onKeyDown = (event) => {
      const action = getKaraokeHotkeyAction(event, scopeRef.current);
      if (action === "toggle-playback") {
        event.preventDefault();
        return handlers.current.onTogglePlay?.();
      }
      if (action === "stop") return handlers.current.onStop?.();
      if (action === "seek-backward" || action === "seek-forward") {
        event.preventDefault();
        const { currentTime: time, duration: length, onSeek: seek } = handlers.current;
        const delta = SEEK_DELTA[action];
        seek?.(
          Math.min(Math.max(0, Number(length) || 0), Math.max(0, (Number(time) || 0) + delta))
        );
      }
    };
    globalThis.addEventListener?.("keydown", onKeyDown);
    return () => globalThis.removeEventListener?.("keydown", onKeyDown);
  }, [handlers, scopeRef]);
}
