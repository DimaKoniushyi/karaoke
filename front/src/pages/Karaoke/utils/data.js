import { translateSaved } from "../../../i18n/runtime";
import { clamp01 } from "../../../utils/math";

export function transposeKey(key, semitones) {
  if (!key) return translateSaved("karaoke.tonalityNotDefined");
  const match = /^([A-G](?:#|b)?)(.*)/i.exec(key.trim());
  if (!match) return key;
  const [, rootText, suffix] = match;
  const root = rootText[0].toUpperCase() + rootText.slice(1);
  const pitch = {
    C: 0,
    "C#": 1,
    Db: 1,
    D: 2,
    "D#": 3,
    Eb: 3,
    E: 4,
    F: 5,
    "F#": 6,
    Gb: 6,
    G: 7,
    "G#": 8,
    Ab: 8,
    A: 9,
    "A#": 10,
    Bb: 10,
    B: 11
  }[root];
  if (pitch == null) return key;
  const shift = Number.isFinite(Number(semitones)) ? Math.round(Number(semitones)) : 0;
  const normalizedPitch = (((pitch + shift) % 12) + 12) % 12;
  return `${
    ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"][normalizedPitch]
  }${suffix}`;
}
export function playbackGain(value) {
  const normalized = clamp01(Number(value) || 0);
  return normalized ** 2;
}
