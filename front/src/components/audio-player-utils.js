import { clampWithFallback } from "../utils/math";
import { formatClockTime } from "../utils/time-format";

const number = (value) => Number(value);
export const clampFinite = clampWithFallback;
export const normalizeAudioDuration = (value) =>
  Number.isFinite(number(value)) && number(value) > 0 ? number(value) : 0;
export const normalizeAudioPosition = (value, duration = null) => {
  if (!Number.isFinite(number(value)) || number(value) <= 0) return 0;
  if (duration == null) return number(value);
  const limit = normalizeAudioDuration(duration);
  return limit ? Math.min(number(value), limit) : 0;
};
export const normalizeAudioVolume = (value) => {
  if (typeof value === "number") return clampFinite(value, 0, 1, 1);
  if (typeof value === "string") return value.trim() ? clampFinite(value, 0, 1, 1) : 1;
  return 1;
};
export const formatAudioTime = (value) => formatClockTime(value, { padMinutes: true });
const tryPlay = async (audio) => {
  for (const key of ["defaultPlaybackRate", "playbackRate"]) {
    try {
      audio[key] = 1;
    } catch {
      // A restricted media implementation may expose this as read-only.
    }
  }
  await Promise.resolve(audio.play());
  return true;
};
export async function toggleAudioPlayback(audio) {
  if (!audio) return false;
  if (!audio.paused) {
    audio.pause();
    return false;
  }
  // Audio players are never participant streams. Clear a stale global mute
  // left by an earlier room session before starting a recording or preview.
  audio.muted = false;
  try {
    await tryPlay(audio);
    return true;
  } catch {
    // A freshly generated file can race Chromium's previous metadata request.
    // Recreate the media pipeline once after an actual playback rejection.
    try {
      audio.pause?.();
      audio.load?.();
      await tryPlay(audio);
      return true;
    } catch {
      return false;
    }
  }
}
