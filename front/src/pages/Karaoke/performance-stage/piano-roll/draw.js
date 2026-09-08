import { clamp } from "../../../../utils/math";
import { connectionLineWidth, noteHeight as computeNoteHeight, noteOpacity, pitchDotRadius } from "./geometry";

const rect = (ctx, x, y, w, h) => {
  ctx.beginPath();
  ctx.roundRect ? ctx.roundRect(x, y, w, h, h / 2) : ctx.rect(x, y, w, h);
};

export function drawPianoRoll(ctx, frame, palette, pitch = {}) {
  const {
    width,
    height,
    keyboard,
    lane,
    rowHeight,
    playhead,
    time,
    y,
    notes = [],
    connections = []
  } = frame;

  const noteHeight = computeNoteHeight(rowHeight);
  const center = rowHeight / 2;

  ctx.clearRect(0, 0, width, height);
  ctx.save();

  ctx.beginPath();
  ctx.rect(keyboard, 0, lane, height);
  ctx.clip();
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.shadowBlur = 0;
  ctx.strokeStyle = palette.hover;
  ctx.lineWidth = connectionLineWidth(rowHeight);

  for (const line of connections) {
    ctx.globalAlpha = line.state === "past" ? 0.3 : 0.72;
    ctx.beginPath();
    ctx.moveTo(line.fromX, line.fromY);
    ctx.lineTo(line.toX, line.toY);
    ctx.stroke();
  }

  for (const note of notes) {
    const current = note.state === "current";
    const top = y(note.note) + (rowHeight - noteHeight) / 2;
    const alpha = noteOpacity(note, time);

    const gradient = ctx.createLinearGradient(0, top, 0, top + noteHeight);
    gradient.addColorStop(0, current ? palette.text : palette.highlight);
    gradient.addColorStop(0.34, palette.hover);
    gradient.addColorStop(1, palette.primary);

    ctx.globalAlpha = alpha;
    ctx.fillStyle = gradient;
    ctx.strokeStyle = palette.highlight;
    ctx.lineWidth = current ? 1.8 : 1.1;
    ctx.shadowColor = current ? palette.hover : "transparent";
    ctx.shadowBlur = current ? 12 : 0;

    rect(ctx, note.left, top, Math.max(1.5, note.right - note.left), noteHeight);
    ctx.fill();
    ctx.stroke();
  }

  ctx.restore();
  ctx.save();

  ctx.globalAlpha = 1;
  ctx.shadowColor = palette.hover;
  ctx.shadowBlur = 10;
  ctx.strokeStyle = palette.hover;
  ctx.lineWidth = 2.2;

  ctx.beginPath();
  ctx.moveTo(playhead, 0);
  ctx.lineTo(playhead, height);
  ctx.stroke();

  if (pitch.detected && Number.isFinite(pitch.midi)) {
    const radius = pitchDotRadius(rowHeight);
    // Pin the dot to the nearest edge instead of hiding it when the singer
    // strays outside the melody's note range -- an octave slip or a genuinely
    // off-pitch note should still show *something*, not vanish silently.
    const dotY = clamp(y(pitch.midi) + center, radius, height - radius);
    ctx.beginPath();
    ctx.arc(playhead, dotY, radius, 0, Math.PI * 2);
    ctx.fillStyle = palette.text;
    ctx.strokeStyle = palette.success;
    ctx.shadowColor = palette.success;
    ctx.lineWidth = 2;
    ctx.fill();
    ctx.stroke();
  }

  ctx.restore();
}
