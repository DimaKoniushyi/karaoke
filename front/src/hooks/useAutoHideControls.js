import { useCallback, useEffect, useRef, useState } from "react";

// Auto-hides on-screen controls after a few seconds of inactivity, revealed
// again by pointer movement or fullscreen changes. Generic (not tied to
// Karaoke) so any full-screen view with a similar "controls fade out" need
// can reuse it.
export default function useAutoHideControls(autoHideEnabled) {
  const [visible, setVisible] = useState(true);
  const activity = useRef(Date.now());
  const pointer = useRef([NaN, NaN]);
  const hidden = useRef(false);

  const showControls = useCallback((manual = false) => {
    if (manual) hidden.current = false;
    if (hidden.current) return;
    activity.current = Date.now();
    setVisible(true);
  }, []);
  const hideControls = useCallback((manual = false) => {
    if (manual) hidden.current = true;
    setVisible(false);
  }, []);
  const revealControls = useCallback(
    (event) => {
      if (hidden.current) return false;
      if (Number.isFinite(event?.clientX) && Number.isFinite(event?.clientY)) {
        const [x, y] = pointer.current;
        if (x === event.clientX && y === event.clientY) return false;
        pointer.current = [event.clientX, event.clientY];
      }
      showControls();
      return true;
    },
    [showControls]
  );

  useEffect(() => {
    if (!autoHideEnabled) return;
    const timer = setInterval(
      () => !hidden.current && setVisible(Date.now() - activity.current < 2200),
      250
    );
    return () => clearInterval(timer);
  }, [autoHideEnabled]);
  useEffect(() => {
    const reveal = () => showControls(true);
    globalThis.document?.addEventListener?.("fullscreenchange", reveal);
    return () => globalThis.document?.removeEventListener?.("fullscreenchange", reveal);
  }, [showControls]);
  useEffect(() => showControls(true), [autoHideEnabled, showControls]);

  return { controlsVisible: visible, hideControls, revealControls, showControls };
}
