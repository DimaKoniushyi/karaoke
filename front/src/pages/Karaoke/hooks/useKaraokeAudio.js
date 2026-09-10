import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import useAsyncQueue from "../../../hooks/useAsyncQueue";
import useMountedRef from "../../../hooks/useMountedRef";
import { usePolling } from "../../../hooks/usePolling";
import { translateSaved as t } from "../../../i18n/runtime";
import { queryKeys } from "../../../query-client";
import { POLLING_INTERVALS } from "../../../runtime-config";
import { AUDIO_SETTINGS_CHANGED_EVENT } from "../../../utils/audioSettingsEvents";
import { getErrorMessage } from "../../../utils/errors";
import { createRoomSyncChannel } from "../../../services/roomSyncChannel";
import { noop, safe } from "../utils/async";
import { getMicrophoneLevel } from "../utils/transport";
import useAudioOutputRouting from "./useAudioOutputRouting";
import useMicrophoneSettings from "./useMicrophoneSettings";

const usePoll = (request, interval, queryKey) => usePolling(request, interval, [], { queryKey });
const notify = (detail) =>
  globalThis.dispatchEvent?.(new CustomEvent(AUDIO_SETTINGS_CHANGED_EVENT, { detail }));

export default function useKaraokeAudio({
  onlineRoom,
  instrumentalRef,
  vocalsRef,
  videoRef,
  setEffectPreset
}) {
  const { room, participants = [], setLocalMonitoring, syncUi } = onlineRoom;
  const mounted = useMountedRef();
  const { run: queue } = useAsyncQueue();
  const [error, setError] = useState(null);
  const [dryMonitor, setDryMonitor] = useState(false);
  const [roomMonitoring, setRoomMonitoringState] = useState(false);
  const roomMonitoringRef = useRef(false);
  const effectMutation = useRef(0);
  const roomEffects = useRef(createRoomSyncChannel());

  const { data: devices } = usePoll(api.listAudioOutputDevices, POLLING_INTERVALS.devices, [
    "audio-output-devices"
  ]);
  const { data: audioSettings } = usePoll(
    api.getAudioSettings,
    POLLING_INTERVALS.devices,
    queryKeys.audioSettings
  );

  const microphone = useMicrophoneSettings({ audioSettings, onError: setError });
  const {
    audioDriver,
    directOutputDeviceId,
    setDirectOutputDeviceId,
    monitoringEnabled,
    setMonitoringEnabled,
    microphoneVolume,
    microphoneEffects,
    setMicrophoneEffects,
    updateMicrophone,
    updateMicrophoneEffects
  } = microphone;
  // With neither monitoring active, the level meter never has anything to
  // show -- polling anyway used to make the backend open the microphone for
  // a real capture probe (see audio_service.check_signal_quality) purely to
  // throw the result away, the same waste the Settings screen used to have.
  const monitoringActive = room ? roomMonitoring : monitoringEnabled;
  const { data: signal } = usePoll(
    monitoringActive ? api.getSignalQuality : () => Promise.resolve(null),
    POLLING_INTERVALS.karaokeSignal,
    ["signal-quality", monitoringActive]
  );
  const monitoringRef = useRef(monitoringEnabled);
  monitoringRef.current = monitoringEnabled;

  const setRoomMonitoring = useCallback(
    (value) => {
      const active = Boolean(value);
      roomMonitoringRef.current = active;
      if (mounted.current) setRoomMonitoringState(active);
      return active;
    },
    [mounted]
  );

  const setNativeMonitoring = useCallback(
    (settings) => {
      const active = Boolean(settings?.monitoring_enabled);
      monitoringRef.current = active;
      if (mounted.current) setMonitoringEnabled(active);
      notify(settings);
      return settings;
    },
    [mounted, setMonitoringEnabled]
  );

  useEffect(() => {
    roomEffects.current = createRoomSyncChannel();
  }, [participants.length, room?.id, room?.selfId]);

  useEffect(() => {
    if (!room || typeof syncUi !== "function") return;
    const state = { volume: microphoneVolume, ...microphoneEffects };
    if (roomEffects.current.shouldSend(state)) safe(() => syncUi({ participantEffects: state }));
  }, [microphoneEffects, microphoneVolume, participants.length, room, syncUi]);

  useAudioOutputRouting({
    audioDriver,
    audioSettings,
    directOutputDeviceId,
    directOutputDevices: devices,
    instrumentalRef,
    vocalsRef,
    videoRef,
    setDirectOutputDeviceId,
    updateMicrophone
  });

  const releaseMonitoring = useCallback(
    () =>
      queue(async () => {
        let failure;
        let settings = null;

        if (roomMonitoringRef.current) {
          try {
            setRoomMonitoring(await setLocalMonitoring(false));
          } catch (error) {
            failure = error;
          }
        }
        if (monitoringRef.current) {
          try {
            settings = setNativeMonitoring(await api.stopDirectMonitoring());
          } catch (error) {
            failure ??= error;
          }
        }

        if (failure) throw failure;
        return settings;
      }),
    [queue, setLocalMonitoring, setNativeMonitoring, setRoomMonitoring]
  );

  const roomKey = room ? `${room.id ?? ""}:${room.selfId ?? ""}:${room.host ? 1 : 0}` : "";

  useEffect(() => {
    setRoomMonitoring(false);
    queue(async () => {
      await safe(() => setLocalMonitoring(false));
      if (room && monitoringRef.current) setNativeMonitoring(await api.stopDirectMonitoring());
    }).catch(noop);
  }, [queue, roomKey, setLocalMonitoring, setNativeMonitoring, setRoomMonitoring]);

  useEffect(() => {
    const release = () => {
      safe(api.releaseDirectMonitoring);
      safe(() => setLocalMonitoring(false));
    };
    globalThis.addEventListener?.("pagehide", release);
    return () => {
      globalThis.removeEventListener?.("pagehide", release);
      release();
    };
  }, [setLocalMonitoring]);

  const onMonitoringChange = useCallback(
    (enabled) =>
      queue(async () => {
        try {
          if (mounted.current) setError(null);

          if (room) {
            if (monitoringRef.current) setNativeMonitoring(await api.stopDirectMonitoring());
            setRoomMonitoring(
              await setLocalMonitoring(Boolean(enabled), {
                volume: microphoneVolume,
                ...microphoneEffects
              })
            );
          } else {
            setNativeMonitoring(
              await (enabled ? api.startDirectMonitoring() : api.stopDirectMonitoring())
            );
          }
        } catch (cause) {
          if (mounted.current) {
            setError(t("karaoke.failedToChangeMicrophoneListening", { 0: getErrorMessage(cause) }));
          }
        }
      }),
    [microphoneEffects, microphoneVolume, mounted, queue, room, setLocalMonitoring, setNativeMonitoring, setRoomMonitoring]
  );

  // Dry monitor is a momentary, unsaved check -- never carry a stale "on"
  // across a stop/restart of monitoring itself.
  useEffect(() => {
    if (!monitoringEnabled && mounted.current) setDryMonitor(false);
  }, [monitoringEnabled, mounted]);

  const onDryMonitorChange = useCallback(
    (enabled) =>
      queue(async () => {
        try {
          const result = await api.setDirectMonitorDry(Boolean(enabled));
          if (mounted.current) setDryMonitor(Boolean(result?.dry_monitor));
          if (enabled && result && !result.supported) {
            setError(t("karaoke.dryMonitorNotSupportedOnAsio"));
          }
        } catch (cause) {
          if (mounted.current) {
            setError(t("karaoke.failedToChangeMicrophoneListening", { 0: getErrorMessage(cause) }));
          }
        }
      }),
    [mounted, queue]
  );

  const saveEffects = useCallback(
    async (preset, patch) => {
      const id = ++effectMutation.current;
      const updated = await updateMicrophoneEffects(patch);
      if (updated !== null && id === effectMutation.current) setEffectPreset(preset);
    },
    [setEffectPreset, updateMicrophoneEffects]
  );

  return {
    ...microphone,
    error,
    microphoneLevel: getMicrophoneLevel(signal),
    monitoringEnabled: room ? roomMonitoring : monitoringEnabled,
    releaseMonitoring,
    onMonitoringChange,
    dryMonitor,
    onDryMonitorChange,
    onEffectChange: (key, value) =>
      setMicrophoneEffects((effects) => ({ ...effects, [key]: value })),
    onEffectCommit: (key, value) => saveEffects("custom", { [key]: value }),
    onApplyEffectPreset: ({ id, echo, reverb, delay }) => saveEffects(id, { echo, reverb, delay })
  };
}
