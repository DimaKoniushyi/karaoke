import { TERMINAL_TRANSFER_STAGES } from "../services/onlineVoiceTransferProtocol";
import { clamp } from "../utils/math";

const ROOM_TRANSFER_BROADCAST_INTERVAL_MS = 500;
export const PARTICIPANT_EFFECT_LIMITS = Object.freeze({
  volume: 2,
  reverb: 1,
  echo: 1,
  delay: 1,
  noise_suppression: 1,
  octave: 1
});
export const normalizeParticipantEffects = (settings = {}) =>
  Object.fromEntries(
    Object.entries(PARTICIPANT_EFFECT_LIMITS).map(([name, maximum]) => {
      const fallback = name === "volume" ? 1 : name === "noise_suppression" ? 0.35 : 0;
      const value = Number(settings?.[name]);
      return [name, clamp(Number.isFinite(value) ? value : fallback, name === "octave" ? -1 : 0, maximum)];
    })
  );
export const normalizeParticipantEffectPatch = (settings = {}) =>
  Object.fromEntries(
    Object.entries(normalizeParticipantEffects(settings)).filter(
      ([key]) => Object.hasOwn(settings, key) && Number.isFinite(Number(settings[key]))
    )
  );
export const shouldBroadcastRoomTransferProgress = (
  previous,
  { commandId, stage, percent },
  now = Date.now()
) =>
  !previous ||
  previous.commandId !== commandId ||
  TERMINAL_TRANSFER_STAGES.has(stage) ||
  (previous.percent !== percent && now - previous.at >= ROOM_TRANSFER_BROADCAST_INTERVAL_MS);
