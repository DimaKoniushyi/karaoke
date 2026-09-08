import { memo, useLayoutEffect } from "react";
import { translateSaved as t } from "../../../i18n/runtime";
import { Box, PianoKeyboard, StudioScrollbars } from "../../../theme/ui";
import {
  filterByTimeRange,
  notesOverlappingWords,
  setPlayheadPosition,
  visibleTimeRange
} from "../model";
import Note from "./note";
import useScroll from "./useScroll";
import Word from "./word";

const EMPTY_SCROLL = {
  clientHeight: 1,
  clientWidth: 1,
  scrollHeight: 1,
  scrollLeft: 0,
  scrollTop: 0,
  scrollWidth: 1
};

function EditorSurface({ controller: c, transport }) {
  const sync = useScroll(c.shellRef, c.surfaceRef);
  const scroll = c.shellRef.current || EMPTY_SCROLL;
  const range = visibleTimeRange(scroll, c);
  const notes = filterByTimeRange(c.notes, range);
  const words = filterByTimeRange(c.words, range);
  const highlighted = notesOverlappingWords(notes, c.words, c.selectedWords);
  const seek = (e) => {
    const { left } = c.surfaceRef.current.getBoundingClientRect() || {};
    transport.seek((e.clientX - left - c.keyboardWidth) / c.zoom);
  };
  useLayoutEffect(() => {
    setPlayheadPosition(c.playheadRef, transport.timeRef.current, c.zoom, { announce: false });
  }, [c.zoom, c.playheadRef, transport.timeRef]);
  return (
    <Box sx={{ position: "relative", flex: 1, minBlockSize: 0, overflow: "hidden" }}>
      <Box
        ref={c.shellRef}
        data-role="editor-scroll-area"
        onWheel={(e) => {
          if (!e.ctrlKey) return;
          e.preventDefault();
          const zoom = e.shiftKey;
          const set = zoom ? c.setZoom : c.setRowHeight;
          const [min, max, speed] = zoom ? [36, 600, 0.2] : [10, 36, 0.02];

          set((v) => Math.max(min, Math.min(max, v - e.deltaY * speed)));
        }}
        sx={{
          position: "absolute",
          inset: 0,
          overflow: "auto",
          scrollbarWidth: "none",
          background: "var(--color-bg-deep)"
        }}
      >
        <Box
          ref={c.surfaceRef}
          data-role="editor-surface"
          onPointerDown={c.startMarquee}
          onPointerMove={c.movePointer}
          onPointerUp={c.endPointer}
          onPointerCancel={c.endPointer}
          onDoubleClick={seek}
          sx={{
            position: "relative",
            width: c.laneWidth,
            height: c.laneHeight,
            backgroundColor: "color-mix(in srgb,var(--color-bg-deep) 94%,var(--color-primary))",
            backgroundImage:
              "linear-gradient(90deg,color-mix(in srgb,var(--color-primary) 10%,transparent) 1px,transparent 1px),linear-gradient(180deg,color-mix(in srgb,var(--color-primary) 13%,transparent) 1px,transparent 1px)",
            backgroundSize: `${c.zoom}px 100%,100% ${c.rowHeight}px`,
            touchAction: "none",
            userSelect: "none"
          }}
        >
          <Box sx={{ position: "sticky", left: 0, width: c.keyboardWidth, height: 0, zIndex: 8 }}>
            <PianoKeyboard
              auditionNote={transport.tone}
              height={c.laneHeight}
              maxMidi={c.maxMidi}
              minMidi={c.minMidi}
              rowHeight={c.rowHeight}
              width={c.keyboardWidth}
            />
          </Box>
          <Box
            aria-label={t("editor.songLyrics")}
            sx={{
              position: "sticky",
              top: 0,
              height: c.rowHeight * 1.45,
              borderBottom: "1px solid color-mix(in srgb,var(--color-primary) 20%,transparent)",
              background: "color-mix(in srgb,var(--color-bg-deep) 82%,transparent)",
              backdropFilter: "blur(var(--space-1))",
              zIndex: 3
            }}
          >
            {words.map((word) => (
              <Word key={word.index} c={c} word={word} />
            ))}
          </Box>
          {notes.map((note) => (
            <Note key={note._id} c={c} note={note} highlighted={highlighted.has(note._id)} />
          ))}
          {c.selectionBox && (
            <Box
              aria-hidden
              sx={{
                position: "absolute",
                left: Math.min(c.selectionBox.x1, c.selectionBox.x2),
                top: Math.min(c.selectionBox.y1, c.selectionBox.y2),
                width: Math.abs(c.selectionBox.x2 - c.selectionBox.x1),
                height: Math.abs(c.selectionBox.y2 - c.selectionBox.y1),
                border: "1px solid var(--color-highlight)",
                background: "color-mix(in srgb,var(--color-primary) 18%,transparent)",
                pointerEvents: "none",
                zIndex: 7
              }}
            />
          )}
          <Box
            ref={c.playheadRef}
            data-role="editor-playhead"
            role="slider"
            tabIndex={0}
            aria-label={t("editor.playbackPosition")}
            aria-valuemin={0}
            aria-valuemax={c.duration}
            aria-valuenow={transport.timeRef.current}
            onPointerDown={(e) => {
              e.stopPropagation();
              e.currentTarget.setPointerCapture?.(e.pointerId);
              seek(e);
            }}
            onPointerMove={(e) => {
              if (e.currentTarget.hasPointerCapture?.(e.pointerId)) seek(e);
            }}
            onKeyDown={(e) => {
              if (!e.key.startsWith("Arrow")) return;
              const step = e.key === "ArrowRight" ? 0.05 : e.key === "ArrowLeft" ? -0.05 : 0;
              if (!step) return;
              e.preventDefault();
              transport.seek(transport.timeRef.current + step);
            }}
            sx={{
              position: "absolute",
              top: 0,
              bottom: 0,
              left: c.keyboardWidth,
              width: "var(--space-3)",
              transform: "translateX(-50%)",
              borderLeft: "calc(var(--hairline) * 2) solid var(--color-primary-hover)",
              filter: "drop-shadow(0 0 var(--space-2) var(--color-primary))",
              cursor: "ew-resize",
              touchAction: "none",
              zIndex: 6
            }}
          />
        </Box>
      </Box>
      <StudioScrollbars
        keyboardWidth={c.keyboardWidth}
        scrollRef={c.shellRef}
        scrollState={scroll}
        sync={sync}
        horizontalZoom={c.zoom}
        verticalZoom={c.rowHeight}
        onHorizontalZoom={c.setZoom}
        onVerticalZoom={c.setRowHeight}
      />
    </Box>
  );
}

export default memo(EditorSurface);
