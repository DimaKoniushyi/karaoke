import { translateSaved } from "../i18n/runtime";
import { clampWithFallback } from "../utils/math";

const SONG_STATUSES = ["queued", "processing", "cancelling", "cancelled", "done", "error"];
export const clampNumber = clampWithFallback;
export function normalizeString(value, fallback = "") {
  if (value == null) return fallback;
  const text = String(value).trim();
  return text || fallback;
}
export function normalizeBoolean(value, fallback = false) {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return Number.isFinite(value) ? value !== 0 : fallback;
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["true", "1", "yes", "on"].includes(normalized)) return true;
    if (["false", "0", "no", "off", ""].includes(normalized)) return false;
  }
  return fallback;
}
export function normalizeNonNegativeNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? number : fallback;
}
export function normalizeSong(song) {
  const source = song && typeof song === "object" ? song : {};
  const rawStatus = normalizeString(source.status).toLowerCase();
  return {
    ...source,
    id: normalizeString(source.id),
    title: normalizeString(source.title, translateSaved("api.untitled")),
    status: SONG_STATUSES.includes(rawStatus) ? rawStatus : "pending",
    progress_percent: clampNumber(source.progress_percent, 0, 100, 0),
    progress_step: normalizeString(source.progress_step),
    error_message: normalizeString(source.error_message) || null
  };
}
export function normalizeSongList(value) {
  return Array.isArray(value) ? value.map(normalizeSong) : [];
}
export function normalizeModel(model) {
  const source = model && typeof model === "object" ? model : {};
  return {
    ...source,
    name: normalizeString(source.name),
    downloaded: normalizeBoolean(source.downloaded),
    selected: normalizeBoolean(source.selected),
    size: normalizeNonNegativeNumber(source.size)
  };
}
export function normalizeRecording(recording) {
  const source = recording && typeof recording === "object" ? recording : {};
  return {
    ...source,
    id: normalizeString(source.id),
    song_id: normalizeString(source.song_id),
    duration_sec: normalizeNonNegativeNumber(source.duration_sec)
  };
}
