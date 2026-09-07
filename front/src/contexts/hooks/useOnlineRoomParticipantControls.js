import { useCallback } from "react";
import { clamp01 } from "../../utils/math";
import { normalizeParticipantEffectPatch } from "../onlineRoomEffects";

export default function useOnlineRoomParticipantControls({
  setMutedPeople,
  mutedPeopleRef,
  applyRemoteAudioMute,
  participantVolumesRef,
  setParticipantVolumes,
  applyParticipantVolume,
  clientRef,
  roomRef,
  setEffectPeople,
  applyParticipantEffects
}) {
  const togglePersonMuted = useCallback(
    (id) => {
      setMutedPeople((items) => {
        const next = new Set(items);
        if (next.has(id)) next.delete(id);
        else next.add(id);
        mutedPeopleRef.current = next;
        queueMicrotask(applyRemoteAudioMute);
        return next;
      });
    },
    // Stryker disable next-line ArrayDeclaration: applyRemoteAudioMute is stable.
    [applyRemoteAudioMute, mutedPeopleRef, setMutedPeople]
  );

  const setParticipantVolume = useCallback(
    (id, value) => {
      const nextValue = clamp01(Number(value) || 0);
      participantVolumesRef.current = { ...participantVolumesRef.current, [id]: nextValue };
      setParticipantVolumes((current) => ({ ...current, [id]: nextValue }));
      applyParticipantVolume(id, nextValue);
    },
    [applyParticipantVolume, participantVolumesRef, setParticipantVolumes]
  );

  const setEffectsLocked = useCallback(
    (locked) => {
      clientRef.current?.send("effect-permission", { locked: Boolean(locked) });
    },
    [clientRef]
  );

  const requestParticipantEffects = useCallback(
    (participantId, patch) => {
      if (!participantId || participantId === roomRef.current?.selfId) return false;
      return clientRef.current?.send("effect-control", {
        targetId: participantId,
        effects: normalizeParticipantEffectPatch(patch)
      });
    },
    [clientRef, roomRef]
  );

  const togglePersonEffects = useCallback(
    (id) => {
      setEffectPeople((items) => {
        const next = new Set(items);
        const enabled = !next.has(id);
        if (enabled) next.add(id);
        else next.delete(id);
        // Ask that singer's sender to select its already-running wet or dry
        // track for this listener. Processing a received MediaStream through
        // another AudioContext added an avoidable render quantum and made
        // duets audibly late on consumer USB devices.
        clientRef.current?.send("signal", {
          targetId: id,
          signal: { effectsEnabled: enabled }
        });
        queueMicrotask(() => applyParticipantEffects(id, false));
        return next;
      });
    },
    // Stryker disable next-line ArrayDeclaration: applyParticipantEffects is stable.
    [applyParticipantEffects, clientRef, setEffectPeople]
  );

  return {
    togglePersonMuted,
    setParticipantVolume,
    setEffectsLocked,
    requestParticipantEffects,
    togglePersonEffects
  };
}
