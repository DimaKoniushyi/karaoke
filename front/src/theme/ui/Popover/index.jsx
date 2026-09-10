import { forwardRef, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import mergeRefs from "../_internal/mergeRefs";
import Card from "../Card";
import "./popover.css";

const Popover = forwardRef(
  (
    {
      open,
      onClose,
      anchorRef,
      placement = "bottom-start",
      offset = 8,
      portal = true,
      children,
      className = "",
      style,
      ...props
    },
    ref
  ) => {
    const popoverRef = useRef(null);
    const [position, setPosition] = useState(null);

    const updatePosition = useCallback(() => {
      const anchor = anchorRef?.current;
      const popover = popoverRef.current;
      if (!anchor || !popover) return;
      const anchorRect = anchor.getBoundingClientRect();
      const popoverRect = popover.getBoundingClientRect();
      const { left: anchorLeft } = anchorRect;
      let top = anchorRect.bottom + offset;
      let left = anchorLeft;
      if (placement === "bottom-end") left = anchorRect.right - popoverRect.width;
      if (placement === "top-start" || placement === "top-end") {
        top = anchorRect.top - popoverRect.height - offset;
        if (placement === "top-end") left = anchorRect.right - popoverRect.width;
      }
      if (placement === "right") {
        top = anchorRect.top + (anchorRect.height - popoverRect.height) / 2;
        left = anchorRect.right + offset;
      }
      if (placement === "left") {
        top = anchorRect.top + (anchorRect.height - popoverRect.height) / 2;
        left = anchorRect.left - popoverRect.width - offset;
      }
      const margin = 8;
      setPosition({
        // +20 nudges the popover down from its anchor; applied before the
        // clamp (not after) so it can never push the popover past the
        // bottom edge of the viewport.
        top: Math.max(margin, Math.min(top + 20, window.innerHeight - popoverRect.height - margin)),
        left: Math.max(margin, Math.min(left, window.innerWidth - popoverRect.width - margin))
      });
    }, [anchorRef, offset, placement]);

    useLayoutEffect(() => {
      if (!open || !anchorRef) return undefined;
      updatePosition();
      const observer =
        typeof ResizeObserver === "function" ? new ResizeObserver(updatePosition) : null;
      if (anchorRef.current) observer?.observe(anchorRef.current);
      if (popoverRef.current) observer?.observe(popoverRef.current);
      window.addEventListener("resize", updatePosition);
      window.addEventListener("scroll", updatePosition, true);
      return () => {
        observer?.disconnect();
        window.removeEventListener("resize", updatePosition);
        window.removeEventListener("scroll", updatePosition, true);
      };
    }, [anchorRef, open, updatePosition]);

    useEffect(() => {
      if (!open || !onClose) return;

      const closeOutside = (event) => {
        const portalLayer = event.target?.closest?.(".ui-popover[id]");
        const portalOwner = portalLayer?.id
          ? [...(popoverRef.current?.querySelectorAll?.("[aria-controls]") || [])].some(
              (control) => control.getAttribute("aria-controls") === portalLayer.id
            )
          : false;
        if (
          !popoverRef.current?.contains(event.target) &&
          !anchorRef?.current?.contains(event.target) &&
          !portalOwner
        ) {
          onClose(event);
        }
      };

      const closeOnEscape = (event) => {
        if (event.key === "Escape") {
          onClose(event);
        }
      };

      document.addEventListener("pointerdown", closeOutside, true);
      document.addEventListener("keydown", closeOnEscape);

      return () => {
        document.removeEventListener("pointerdown", closeOutside, true);
        document.removeEventListener("keydown", closeOnEscape);
      };
    }, [anchorRef, open, onClose]);

    const content = (
      <Card
        variant="laser"
        tilt={false}
        ref={mergeRefs(ref, popoverRef)}
        className={`ui-popover ${className}`.trim()}
        data-open={open || undefined}
        sx={{ containerType: "normal" }}
        style={{
          ...style,
          ...(anchorRef && {
            position: "fixed",
            top: position?.top ?? 0,
            left: position?.left ?? 0,
            visibility: position ? "visible" : "hidden"
          })
        }}
        {...props}
      >
        {children}
      </Card>
    );
    return portal && typeof document !== "undefined"
      ? createPortal(content, document.body)
      : content;
  }
);

export default Popover;
