import { useCallback, useEffect, useRef } from "react";
import { connectVoiceEffects } from "../../services/voiceEffects";
import { clamp01 as clampUnit } from "../../utils/math";

const clamp01 = (value) => clampUnit(Number(value) || 0);
const closeContext = (context) => {
  try {
    const result = context?.close?.();
    result?.catch?.(() => {});
  } catch {
    // Closing an already-closed Web Audio context is harmless.
  }
};
const routeMediaOutput = (target, deviceId) => {
  if (typeof target?.setSinkId !== "function") return null;
  try {
    return Promise.resolve(target.setSinkId(deviceId || "")).catch(() => false);
  } catch {
    return Promise.resolve(false);
  }
};

export default function useOnlineRoomAudio({
  mutedPeopleRef,
  roomSoundMutedRef,
  roomUiRef,
  participantVolumesRef,
  startSpeakingMeter,
  stopSpeakingMeter,
  voiceRef
}) {
  const remoteAudioRef = useRef(new Map());
  const remoteEffectsRef = useRef(new Map());
  const remoteEffectVersionsRef = useRef(new Map());
  const localMonitorRef = useRef(null);
  const outputDeviceIdRef = useRef("");
  // One shared AudioContext for every remote participant's effects graph,
  // not one each -- a room with several participants who all have effects
  // enabled used to open a separate AudioContext (its own render thread)
  // per participant. Lazily created, reused across participants, and never
  // closed on a single participant leaving (see removeRemoteAudio) since
  // others may still be using it.
  const sharedEffectsContextRef = useRef(null);
  const getSharedEffectsContext = useCallback(() => {
    if (sharedEffectsContextRef.current?.state !== "closed" && sharedEffectsContextRef.current) {
      return sharedEffectsContextRef.current;
    }
    const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
    if (!AudioContextClass) return null;
    sharedEffectsContextRef.current = new AudioContextClass({ latencyHint: 0 });
    return sharedEffectsContextRef.current;
  }, []);

  const applyOutputRoute = useCallback(
    (deviceId) => {
      const normalized = typeof deviceId === "string" ? deviceId : "";
      outputDeviceIdRef.current = normalized;
      remoteAudioRef.current.forEach((audio) => routeMediaOutput(audio, normalized));
      remoteEffectsRef.current.forEach(({ context }) => routeMediaOutput(context, normalized));
      routeMediaOutput(voiceRef.current, normalized);
      routeMediaOutput(localMonitorRef.current?.context, normalized);
    },
    [voiceRef]
  );

  useEffect(() => {
    const route = (event) => applyOutputRoute(event.detail?.deviceId);
    globalThis.addEventListener?.("audio-output-route-changed", route);
    return () => globalThis.removeEventListener?.("audio-output-route-changed", route);
  }, [applyOutputRoute]);

  const applyRemoteAudioMute = useCallback(() => {
    for (const [participantId, audio] of remoteAudioRef.current) {
      const muted = roomSoundMutedRef.current || mutedPeopleRef.current.has(participantId);
      const effectGraph = remoteEffectsRef.current.get(participantId);
      const volume = Math.max(
        0,
        Math.min(1, Number(participantVolumesRef.current?.[participantId] ?? 1))
      );
      const ownerVolume = Math.max(
        0,
        Math.min(2, Number(roomUiRef.current.effectsByParticipant?.[participantId]?.volume ?? 1))
      );
      audio.muted = muted || Boolean(effectGraph);
      audio.volume = effectGraph ? 1 : Math.min(1, volume);
      if (effectGraph) effectGraph.master.gain.value = muted ? 0 : volume * ownerVolume;
    }
  }, [mutedPeopleRef, participantVolumesRef, roomSoundMutedRef, roomUiRef]);

  const setParticipantVolume = useCallback(
    (participantId, value) => {
      participantVolumesRef.current = {
        ...(participantVolumesRef.current || {}),
        [participantId]: clamp01(value)
      };
      applyRemoteAudioMute();
    },
    [applyRemoteAudioMute, participantVolumesRef]
  );

  const removeRemoteAudio = useCallback(
    (participantId) => {
      // Deleting (rather than just bumping) still invalidates any in-flight
      // applyParticipantEffects activation for this participant -- it reads
      // undefined back, which never equals a real (>=1) effectVersion, so a
      // stale activation still correctly closes its context instead of
      // attaching. Participant ids are fresh crypto.randomUUID()s per
      // connection (see cloudflare/src/worker.js), never reused, so a
      // future join can't collide with a version number left behind here.
      // OnlineRoomProvider mounts once for the whole app session, so
      // without this the map only ever grew, one entry per participant who
      // ever passed through any room, for the process's entire lifetime.
      remoteEffectVersionsRef.current.delete(participantId);
      stopSpeakingMeter(participantId);
      const effectGraph = remoteEffectsRef.current.get(participantId);
      remoteEffectsRef.current.delete(participantId);
      // The effects context is shared across every participant now (see
      // sharedEffectsContextRef) -- only this participant's own nodes are
      // torn down, never the context itself, since others may still be
      // using it.
      effectGraph?.dispose?.();
      const audio = remoteAudioRef.current.get(participantId);
      if (!audio) return;
      audio.pause();
      audio.srcObject = null;
      audio.remove();
      remoteAudioRef.current.delete(participantId);
    },
    [stopSpeakingMeter]
  );

  const removeAllRemoteAudio = useCallback(() => {
    [...remoteAudioRef.current.keys()].forEach(removeRemoteAudio);
  }, [removeRemoteAudio]);

  const getRemoteVoiceStreams = useCallback(
    () =>
      [...remoteAudioRef.current.values()]
        .map((audio) => audio.srcObject)
        .filter((stream) =>
          stream?.getAudioTracks?.().some((track) => track.readyState === "live")
        ),
    []
  );

  const attachRemoteStream = useCallback(
    (participantId, stream, onPlayBlocked) => {
      removeRemoteAudio(participantId);
      const audio = document.createElement("audio");
      audio.dataset.onlineRoomParticipant = participantId;
      audio.autoplay = true;
      audio.playsInline = true;
      audio.srcObject = stream;
      audio.style.display = "none";
      document.body.append(audio);
      remoteAudioRef.current.set(participantId, audio);
      startSpeakingMeter(participantId, stream);
      applyRemoteAudioMute();
      const routed = routeMediaOutput(audio, outputDeviceIdRef.current);
      const play = () => audio.play().catch(() => onPlayBlocked?.());
      if (routed) routed.finally(play);
      else play();
      return audio;
    },
    [applyRemoteAudioMute, removeRemoteAudio, startSpeakingMeter]
  );

  const applyParticipantEffects = useCallback(
    (participantId, enabled) => {
      const effectVersion = (remoteEffectVersionsRef.current.get(participantId) || 0) + 1;
      remoteEffectVersionsRef.current.set(participantId, effectVersion);
      const previous = remoteEffectsRef.current.get(participantId);
      remoteEffectsRef.current.delete(participantId);
      previous?.dispose?.();
      const audio = remoteAudioRef.current.get(participantId);
      const stream = audio?.srcObject;
      if (!enabled || !stream) {
        applyRemoteAudioMute();
        return;
      }
      const context = getSharedEffectsContext();
      if (!context) {
        applyRemoteAudioMute();
        return;
      }
      const effects = roomUiRef.current.effectsByParticipant?.[participantId] || {};
      routeMediaOutput(context, outputDeviceIdRef.current);
      const source = context.createMediaStreamSource(stream);
      const master = context.createGain();
      master.gain.value = 1;
      source.connect(master);

      const voiceEffects = connectVoiceEffects(context, source, master, effects);
      master.connect(context.destination);
      const dispose = () => {
        try {
          source.disconnect();
        } catch {
          // Already detached.
        }
        try {
          master.disconnect();
        } catch {
          // Already detached.
        }
        voiceEffects.close();
      };
      const activate = () => {
        if (
          remoteEffectVersionsRef.current.get(participantId) !== effectVersion ||
          remoteAudioRef.current.get(participantId)?.srcObject !== stream
        ) {
          dispose();
          return;
        }
        remoteEffectsRef.current.set(participantId, { context, master, dispose });
        applyRemoteAudioMute();
      };
      if (typeof context.resume === "function") {
        Promise.resolve(context.resume())
          .then(activate)
          .catch(() => {
            dispose();
            applyRemoteAudioMute();
          });
      } else activate();
    },
    [applyRemoteAudioMute, getSharedEffectsContext, roomUiRef]
  );

  const stopLocalMonitoring = useCallback(() => {
    const monitor = localMonitorRef.current;
    localMonitorRef.current = null;
    if (!monitor) return;
    if (monitor.direct) {
      monitor.voice.setLocalMonitoring(false);
      return;
    }
    try {
      monitor.source.disconnect();
      monitor.gain.disconnect();
    } catch {
      // Already disconnected.
    }
    closeContext(monitor.context);
  }, []);

  const setLocalMonitoring = useCallback(
    async (enabled, effects = {}) => {
      if (!enabled) {
        stopLocalMonitoring();
        return false;
      }
      if (localMonitorRef.current) return true;
      const voice = voiceRef.current;
      if (!voice) return false;
      if (typeof voice.setLocalMonitoring === "function") {
        await routeMediaOutput(voice, outputDeviceIdRef.current);
        const active = await voice.setLocalMonitoring(true, effects);
        if (voiceRef.current !== voice) {
          voice.setLocalMonitoring(false);
          return false;
        }
        if (active) localMonitorRef.current = { voice, direct: true };
        return Boolean(active);
      }
      const stream = await voice.start();
      if (voiceRef.current !== voice) {
        stream.getTracks().forEach((track) => track.stop());
        return false;
      }
      const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!AudioContextClass) return false;
      let context;
      try {
        context = new AudioContextClass({ latencyHint: 0 });
        await routeMediaOutput(context, outputDeviceIdRef.current);
        const source = context.createMediaStreamSource(stream);
        const gain = context.createGain();
        gain.gain.value = Math.max(0, Math.min(2, Number(effects.volume ?? 1)));
        // voice.start() returns the already processed stream from the central
        // microphone service. Applying the channel strip again would gate and
        // compress the singer twice.
        source.connect(gain);

        connectVoiceEffects(context, source, gain, effects);
        gain.connect(context.destination);
        await context.resume?.();
        if (voiceRef.current !== voice) {
          closeContext(context);
          return false;
        }
        localMonitorRef.current = { context, source, gain };
        return true;
      } catch {
        closeContext(context);
        return false;
      }
    },
    [stopLocalMonitoring, voiceRef]
  );

  return {
    applyParticipantEffects,
    applyRemoteAudioMute,
    attachRemoteStream,
    getRemoteVoiceStreams,
    removeAllRemoteAudio,
    removeRemoteAudio,
    setParticipantVolume,
    setLocalMonitoring,
    stopLocalMonitoring
  };
}
