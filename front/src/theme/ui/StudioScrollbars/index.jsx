import { translateSaved as tr } from "../../../i18n/runtime";
import { MoveHorizontal, MoveVertical } from "lucide-react";
import { useCallback, useRef } from "react";
import Box from "../Box";
import Slider from "../Slider";
import Stack from "../Stack";

const axisData = (axis) =>
  axis === "x"
    ? { client: "clientWidth", scroll: "scrollLeft", size: "scrollWidth" }
    : { client: "clientHeight", scroll: "scrollTop", size: "scrollHeight" };

function ScrollTrack({ axis, scrollRef, state, sync }) {
  const dragRef = useRef(null);
  const horizontal = axis === "x";
  const { client, scroll, size } = axisData(axis);
  const viewport = state[client] || 1;
  const content = state[size] || viewport;
  const maximum = Math.max(0, content - viewport);
  const thumb = Math.min(100, Math.max((viewport / content) * 100, 1.5));
  const offset = maximum ? (state[scroll] / maximum) * (100 - thumb) : 0;
  const move = useCallback(
    (event) => {
      const drag = dragRef.current;
      const shell = scrollRef.current;
      if (!drag || !shell) return;
      const pointer = horizontal ? event.clientX : event.clientY;
      const next = drag.start + ((pointer - drag.pointer) / drag.span) * maximum;
      shell[scroll] = Math.max(0, Math.min(maximum, next));
      sync();
    },
    [horizontal, maximum, scroll, scrollRef, sync]
  );
  const jump = (event) => {
    const shell = scrollRef.current;
    if (!shell || event.target.dataset.role === "studio-scroll-thumb") return;
    const rect = event.currentTarget.getBoundingClientRect();
    const span = horizontal ? rect.width : rect.height;
    const pointer = horizontal ? event.clientX - rect.left : event.clientY - rect.top;
    shell[scroll] = Math.max(0, Math.min(maximum, (pointer / span) * maximum));
    sync();
  };
  return (
    <Box
      role="scrollbar"
      aria-label={horizontal ? tr("common.scroll.horizontal") : tr("common.scroll.vertical")}
      aria-orientation={horizontal ? "horizontal" : "vertical"}
      aria-valuemin={0}
      aria-valuemax={maximum}
      aria-valuenow={Math.round(state[scroll] || 0)}
      tabIndex={0}
      onPointerDown={jump}
      onKeyDown={(event) => {
        const shell = scrollRef.current;
        const direction = horizontal
          ? { ArrowLeft: -1, ArrowRight: 1 }
          : { ArrowUp: -1, ArrowDown: 1 };
        if (!shell || !direction[event.key]) return;
        event.preventDefault();
        shell[scroll] += direction[event.key] * viewport * 0.1;
        sync();
      }}
      sx={{
        position: "relative",
        flex: 1,
        minInlineSize: 0,
        minBlockSize: 0,
        inlineSize: horizontal ? "auto" : "var(--space-2)",
        blockSize: horizontal ? "var(--space-2)" : "auto",
        border: "1px solid color-mix(in srgb, var(--color-primary) 15%, var(--color-text) 8%)",
        borderRadius: "var(--radius-pill)",
        background: "color-mix(in srgb, var(--color-text) 4%, transparent)",
        boxShadow: "inset 0 1px var(--space-1) color-mix(in srgb, var(--color-bg-deep) 60%, transparent)",
        outline: "none",
        cursor: "pointer"
      }}
    >
      <Box
        data-role="studio-scroll-thumb"
        onPointerDown={(event) => {
          event.preventDefault();
          event.stopPropagation();
          const rect = event.currentTarget.parentElement.getBoundingClientRect();
          dragRef.current = {
            pointer: horizontal ? event.clientX : event.clientY,
            span: horizontal ? rect.width : rect.height,
            start: scrollRef.current?.[scroll] || 0
          };
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerMove={move}
        onPointerUp={(event) => {
          dragRef.current = null;
          event.currentTarget.releasePointerCapture?.(event.pointerId);
        }}
        onPointerCancel={() => {
          dragRef.current = null;
        }}
        sx={{
          position: "absolute",
          inset: horizontal ? `-1px auto -1px ${offset}%` : `${offset}% -1px auto -1px`,
          inlineSize: horizontal ? `${thumb}%` : "var(--space-2)",
          blockSize: horizontal ? "var(--space-2)" : `${thumb}%`,
          minInlineSize: horizontal ? "var(--space-6)" : 0,
          minBlockSize: horizontal ? 0 : "var(--space-6)",
          borderRadius: "var(--radius-pill)",
          background: "linear-gradient(180deg, var(--color-primary-hover), var(--color-primary))",
          boxShadow:
            "0 0 var(--space-2) color-mix(in srgb, var(--color-primary) 32%, transparent), inset 0 1px color-mix(in srgb, var(--color-white) 22%, transparent)",
          cursor: "grab",
          touchAction: "none"
        }}
      />
    </Box>
  );
}

function Zoom({ axis, value, onChange }) {
  const horizontal = axis === "x";
  const Icon = horizontal ? MoveHorizontal : MoveVertical;
  return (
    <Stack
      as="label"
      direction={horizontal ? "row" : "column"}
      align="center"
      gap="var(--space-1)"
      sx={{
        inlineSize: horizontal ? "calc(var(--space-16) * 2 + var(--space-2))" : "var(--space-4)",
        blockSize: horizontal ? "var(--space-4)" : "calc(var(--space-16) + var(--space-10))",
        flex: "none"
      }}
    >
      <Icon size="1em" aria-hidden="true" />
      <Slider
        aria-label={horizontal ? tr("common.zoom.horizontal") : tr("common.zoom.vertical")}
        min={horizontal ? 36 : 10}
        max={horizontal ? 600 : 36}
        step="1"
        value={value}
        onChange={onChange}
        showValue={false}
        controlSx={{
          inlineSize: horizontal ? "auto" : "calc(var(--space-16) + var(--space-5))",
          flex: horizontal ? 1 : "none",
          minInlineSize: 0,
          minBlockSize: 0,
          blockSize: "var(--space-4)",
          transform: horizontal ? undefined : "translateY(var(--space-8)) rotate(-90deg)",
          transformOrigin: "center"
        }}
        sx={{
          inlineSize: "100%",
          minInlineSize: 0,
          blockSize: "var(--space-4)",
          margin: 0,
          "--slider-track": "var(--space-1)",
          "--slider-thumb": "var(--space-3)",
          "--slider-fill": "var(--color-text)",
          "--slider-fill-end": "var(--color-text)",
          "--slider-rest": "var(--color-text)",
          "--slider-thumb-bg": "var(--color-primary)",
          "--slider-thumb-border": "var(--color-primary)"
        }}
      />
    </Stack>
  );
}

export default function StudioScrollbars({
  keyboardWidth,
  scrollRef,
  scrollState,
  sync,
  horizontalZoom,
  verticalZoom,
  onHorizontalZoom,
  onVerticalZoom
}) {
  return (
    <>
      <Stack
        aria-label={tr("common.scroll.horizontal")}
        direction="row"
        align="center"
        gap="var(--space-1)"
        sx={{
          position: "absolute",
          inset: `auto var(--space-4) var(--space-1) ${keyboardWidth + 5}px`,
          zIndex: 20,
          inlineSize: "auto",
          blockSize: "var(--space-4)",
          pointerEvents: "auto"
        }}
      >
        <ScrollTrack axis="x" scrollRef={scrollRef} state={scrollState} sync={sync} />
        <Zoom axis="x" value={horizontalZoom} onChange={onHorizontalZoom} />
      </Stack>
      <Stack
        aria-label={tr("common.scroll.vertical")}
        align="center"
        gap="var(--space-1)"
        sx={{
          position: "absolute",
          inset: "var(--space-1) var(--space-1) var(--space-4) auto",
          zIndex: 20,
          inlineSize: "var(--space-4)",
          pointerEvents: "auto"
        }}
      >
        <ScrollTrack axis="y" scrollRef={scrollRef} state={scrollState} sync={sync} />
        <Zoom axis="y" value={verticalZoom} onChange={onVerticalZoom} />
      </Stack>
    </>
  );
}
