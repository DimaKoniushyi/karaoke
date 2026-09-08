import { useCallback, useEffect, useRef, useState } from "react";

// Shared "hover-intent" popover state: opens immediately, closes after a
// short delay so moving the pointer from the anchor onto the popover itself
// does not flicker it shut.
export default function useHoverPopover(delay = 120) {
  const anchorRef = useRef(null);
  const timerRef = useRef(null);
  const [open, setOpen] = useState(false);

  const show = useCallback(() => {
    clearTimeout(timerRef.current);
    setOpen(true);
  }, []);
  const hideSoon = useCallback(() => {
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => setOpen(false), delay);
  }, [delay]);
  useEffect(() => () => clearTimeout(timerRef.current), []);

  return { open, setOpen, anchorRef, show, hideSoon };
}
