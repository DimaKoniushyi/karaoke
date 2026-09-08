const A4_MIDI = 69;
const A4_FREQUENCY = 440;

export function midiToFrequency(midi) {
  const value = Number(midi);
  if (!Number.isFinite(value)) return null;
  return A4_FREQUENCY * 2 ** ((value - A4_MIDI) / 12);
}

// Collapses a key name to karaoke-style compact notation ("A minor" -> "Am",
// "C major"/"C maj" -> "Cmaj"). Only the trailing major/minor qualifier is
// collapsed, so a longer/unrelated string is returned unchanged instead of
// being corrupted. `fallback` is returned as-is for an empty key.
export function formatCompactKey(key, fallback = "—") {
  const text = typeof key === "string" ? key.trim() : "";
  if (!text) return fallback;
  return text.replace(/\s+(major|maj)$/i, "maj").replace(/\s+(minor|min)$/i, "m");
}
