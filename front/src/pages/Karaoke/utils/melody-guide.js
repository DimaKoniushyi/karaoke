import { clamp01 } from "../../../utils/math";
import { midiToFrequency } from "../../../utils/music";

export { midiToFrequency };

export function findActiveMelodyNote(notes, position) {
  const time = Number(position);
  if (!Array.isArray(notes) || !Number.isFinite(time)) return null;
  return (
    notes.find((note) => {
      const start = Number(note?.start);
      const end = Number(note?.end);
      return time >= start && time < end;
    }) || null
  );
}

export function getMelodyGuideState({ notes, position, keyShift = 0, volume = 0 }) {
  const safeVolume = clamp01(Number(volume) || 0);
  const note = findActiveMelodyNote(notes, position);
  if (!note || safeVolume <= 0) return { active: false, note: null, frequency: null, gain: 0.0001 };

  const midi = note.note + (Number(keyShift) || 0);
  const frequency = midiToFrequency(midi);
  if (!Number.isFinite(frequency))
    return { active: false, note: null, frequency: null, gain: 0.0001 };

  return { active: true, note, frequency, gain: 0.3 * safeVolume ** 1.65 };
}
