import { isRecord, readJsonStorage, writeJsonStorage } from "./storage";

const KEY = "karaoke-audio-preferences";
const DEVICE = "default";
export const DEFAULT_AUDIO_PREFERENCES = Object.freeze({
  monitorInputDeviceId: DEVICE
});
const deviceId = (value) => (typeof value === "string" && value.trim() ? value : DEVICE);
const normalize = (value = {}) => ({
  monitorInputDeviceId: deviceId(value.monitorInputDeviceId)
});
export const getAudioPreferences = () => normalize(readJsonStorage(KEY));
export function saveAudioPreferences(patch) {
  const value = normalize({ ...getAudioPreferences(), ...(isRecord(patch) ? patch : {}) });
  writeJsonStorage(KEY, value);
  return value;
}
