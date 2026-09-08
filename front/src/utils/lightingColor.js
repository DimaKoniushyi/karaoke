import { clampWithFallback } from "./math";

const clamp01 = (value) => clampWithFallback(value, 0, 1, 0);

export function lightingColor(hex, brightness, level, mode) {
  const base = /^#[a-f\d]{6}$/i.test(hex) ? hex : "#ffffff";
  const gain = clamp01(brightness) * (mode === "theme" ? 1 : clamp01(level));
  return [1, 3, 5].map((offset) => Math.round(parseInt(base.slice(offset, offset + 2), 16) * gain));
}

export function musicLightingColor(brightness, level, phase) {
  const hue = (((Number(phase) || 0) % 1) + 1) % 1;
  const sector = hue * 6;
  const chroma = 1;
  const secondary = chroma * (1 - Math.abs((sector % 2) - 1));
  const colors = [
    [chroma, secondary, 0],
    [secondary, chroma, 0],
    [0, chroma, secondary],
    [0, secondary, chroma],
    [secondary, 0, chroma],
    [chroma, 0, secondary]
  ];
  const gain = clamp01(brightness) * (0.16 + clamp01(level) * 0.84);
  return colors[Math.floor(sector) % colors.length].map((channel) =>
    Math.round(channel * gain * 255)
  );
}

export const sameLightingPalette = (left, right) =>
  Array.isArray(left) &&
  Array.isArray(right) &&
  left.length === right.length &&
  left.every((color, index) => color === right[index]);

const parseColor = (hex) => {
  const value = /^#[a-f\d]{6}$/i.test(hex || "") ? hex : "#ffffff";
  return [1, 3, 5].map((offset) => parseInt(value.slice(offset, offset + 2), 16));
};

/**
 * Build a slow bass envelope constrained to the active theme palette.
 * The channel slew limit prevents a single analyzer spike from becoming a
 * full-brightness hardware flash, including on keyboards with coarse firmware.
 */
export function advanceMusicLighting(
  previous,
  sample,
  palette,
  brightness,
  elapsedMs = 80,
  sensitivity = 1
) {
  const initializing = !previous;
  const state = previous || { envelope: 0, rgb: [0, 0, 0] };
  const active = !!sample?.active;
  // Broadcast radio is heavily compressed: its useful bass envelope normally
  // occupies only about 0.05..0.25 of an FFT byte spectrum. Mapping that tiny
  // range directly to LED brightness made a playing keyboard look static.
  const measuredLevel = clamp01(sample?.level);
  const transient = !!sample?.transient;
  const responseSensitivity = Math.min(2, Math.max(0.25, Number(sensitivity) || 1));
  const targetLevel = active
    ? transient
      ? clamp01(measuredLevel * responseSensitivity)
      : clamp01((measuredLevel - 0.025) / 0.24) ** 1.5
    : 0;
  const milliseconds = Math.min(250, Math.max(16, Number(elapsedMs) || 80));
  const response =
    1 -
    Math.exp(
      -milliseconds /
        (transient
          ? targetLevel > state.envelope
            ? 20
            : 40
          : targetLevel > state.envelope
            ? 140
            : 520)
    );
  const envelope = state.envelope + (targetLevel - state.envelope) * response;
  const colors = (Array.isArray(palette) ? palette : [palette]).filter(Boolean);
  const primary = parseColor(colors[0]);
  // Keep a dim theme-colored floor even between tracks/analyser dropouts.
  // Releasing control here would restart the keyboard firmware's rainbow mode.
  const gain =
    clamp01(brightness) *
    (active ? (transient ? 0.42 + envelope * 0.52 : 0.52 + envelope * 0.32) : 0.32);
  // Music changes only the brightness of the selected theme color. Mixing a
  // secondary palette hue on each hit looked like random RGB switching on
  // keyboards whose firmware updates all keys as one coarse frame.
  const target = primary.map((channel) => Math.round(channel * gain));
  const limit = 8 * (milliseconds / 80);
  const rgb = target.map((channel, index) => {
    if (initializing) return channel;
    const before = Number(state.rgb?.[index]) || 0;
    return Math.round(before + Math.max(-limit, Math.min(limit, channel - before)));
  });
  return { rgb, state: { envelope, rgb } };
}

export function musicLightingZones(rgb, envelope = 0) {
  const pulse = clamp01(envelope);
  const gains = [
    0.56 + pulse * 0.2,
    0.74 + pulse * 0.16,
    1,
    0.74 + pulse * 0.16,
    0.56 + pulse * 0.2
  ];
  return gains.map((gain) => rgb.map((channel) => Math.round(clamp01(channel / 255) * gain * 255)));
}
