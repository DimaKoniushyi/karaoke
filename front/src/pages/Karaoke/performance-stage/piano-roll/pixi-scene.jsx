import { Graphics, Stage, useTick } from "@pixi/react";
import "@pixi/unsafe-eval";
import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import { clamp } from "../../../../utils/math";
import {
  connectionLineWidth,
  noteHeight as computeNoteHeight,
  noteOpacity,
  pianoPitchRange,
  pianoRollFrame,
  pitchDotRadius
} from "./geometry";

const options = {
  antialias: true,
  backgroundAlpha: 0,
  autoDensity: true,
  resolution: Math.min(globalThis.devicePixelRatio || 1, 2)
};

const style = {
  display: "block",
  width: "100%",
  height: "100%"
};

function RollGraphics({
  currentTime,
  currentTimeRef,
  isPitchDetected,
  notes,
  palette,
  size,
  sung
}) {
  const graphics = useRef();
  const range = useMemo(() => pianoPitchRange(notes, sung), [notes, sung]);

  const draw = useCallback(
    (time) => {
      const g = graphics.current;
      if (!g) return;

      const frame = pianoRollFrame(notes, time, size, range);
      const { rowHeight, playhead } = frame;

      g.clear();

      for (const line of frame.connections ?? []) {
        g.lineStyle(
          connectionLineWidth(rowHeight),
          palette.hover,
          line.state === "past" ? 0.3 : 0.72
        )
          .moveTo(line.fromX, line.fromY)
          .lineTo(line.toX, line.toY);
      }

      const noteHeight = computeNoteHeight(rowHeight);
      for (const note of frame.notes) {
        const opacity = noteOpacity(note, frame.time);

        g.lineStyle(note.state === "current" ? 1.8 : 1.1, palette.highlight, opacity)
          .beginFill(note.state === "current" ? palette.hover : palette.primary, opacity)
          .drawRoundedRect(
            note.left,
            frame.y(note.note) + (rowHeight - noteHeight) / 2,
            Math.max(1.5, note.right - note.left),
            noteHeight,
            noteHeight / 2
          )
          .endFill();
      }

      g.lineStyle(2.2, palette.hover).moveTo(playhead, 0).lineTo(playhead, frame.height);

      if (isPitchDetected && Number.isFinite(sung)) {
        const radius = pitchDotRadius(rowHeight);
        // Pin the dot to the nearest edge instead of hiding it when the singer
        // strays outside the melody's note range -- an octave slip or a
        // genuinely off-pitch note should still show *something*, not vanish.
        const dotY = clamp(frame.y(sung) + rowHeight / 2, radius, frame.height - radius);
        g.lineStyle(2, palette.success).beginFill(palette.text).drawCircle(playhead, dotY, radius).endFill();
      }
    },
    [notes, size, range, palette, sung, isPitchDetected]
  );

  useLayoutEffect(
    () => draw(Number(currentTimeRef?.current ?? currentTime) || 0),
    [currentTime, currentTimeRef, draw]
  );

  useTick(() => {
    if (currentTimeRef) draw(Number(currentTimeRef.current) || 0);
  });

  return <Graphics ref={graphics} />;
}

export default function PixiPianoRoll({ size, ...props }) {
  return (
    <Stage width={size.width} height={size.height} options={options} style={style}>
      <RollGraphics {...props} size={size} />
    </Stage>
  );
}
