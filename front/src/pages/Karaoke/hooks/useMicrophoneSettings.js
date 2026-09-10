import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import useAsyncQueue from "../../../hooks/useAsyncQueue";
import useMountedRef from "../../../hooks/useMountedRef";
import { usePolling } from "../../../hooks/usePolling";
import { translateSaved as t } from "../../../i18n/runtime";
import { getAudioPreferences } from "../../../utils/audio-preferences";
import { AUDIO_SETTINGS_CHANGED_EVENT } from "../../../utils/audioSettingsEvents";
import { getErrorMessage } from "../../../utils/errors";
import { normalizeAudioEffects, normalizeAudioRuntimeSettings } from "../utils/audio-settings";

const EFFECT_KEYS = ["reverb", "echo", "delay", "noise_suppression", "octave"];
const DEFAULT_EFFECTS = normalizeAudioEffects({});
const DEFAULT_RUNTIME = {
  volume: 1,
  audioDriver: "auto",
  outputDeviceId: "",
  monitoringEnabled: false
};
const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const notify = (detail) =>
  globalThis.dispatchEvent?.(new CustomEvent(AUDIO_SETTINGS_CHANGED_EVENT, { detail }));

export default function useMicrophoneSettings({ audioSettings, onError }) {
  const [runtime, setRuntime] = useState(DEFAULT_RUNTIME);
  const [microphoneEffects, setMicrophoneEffects] = useState(DEFAULT_EFFECTS);
  // Not user-configurable yet -- read once at mount, matching the stored
  // default until Settings grows a control for it.
  const [monitorInputDeviceId] = useState(
    () => getAudioPreferences().monitorInputDeviceId || "default"
  );
  const confirmedEffects = useRef(DEFAULT_EFFECTS);
  const sequence = useRef(0);
  const pending = useRef(0);
  const reportedMonitorError = useRef(null);
  const mounted = useMountedRef();
  const { run: enqueue } = useAsyncQueue();

  const directMonitor = usePolling(
    () => (runtime.monitoringEnabled ? api.getDirectMonitorStatus() : Promise.resolve(null)),
    runtime.monitoringEnabled ? 1000 : 0,
    [runtime.monitoringEnabled]
  );

  const reportError = useCallback(
    (error) =>
      onError?.(
        t("karaoke.failedToSaveMicrophoneSettings", {
          0: getErrorMessage(error, t("room.transfer.unknownError"))
        })
      ),
    [onError]
  );

  const applyRuntime = useCallback((settings, partial = false) => {
    if (!settings) return;
    const next = normalizeAudioRuntimeSettings(settings);

    setRuntime((current) => {
      if (!partial) return next;
      return {
        volume: has(settings, "volume") ? next.volume : current.volume,
        audioDriver: has(settings, "audio_driver") ? next.audioDriver : current.audioDriver,
        outputDeviceId: has(settings, "output_device_id") ? next.outputDeviceId : current.outputDeviceId,
        monitoringEnabled: has(settings, "monitoring_enabled")
          ? next.monitoringEnabled
          : current.monitoringEnabled
      };
    });
  }, []);

  useEffect(() => {
    const status = directMonitor.data;
    if (status?.state !== "error") return;
    const key = status.request_id ?? status.error;
    if (reportedMonitorError.current === key) return;
    reportedMonitorError.current = key;
    onError?.(t("karaoke.couldNotEnableMonitoring", { 0: status.error }));
  }, [directMonitor.data, onError]);

  useEffect(() => {
    const sync = ({ detail }) => {
      if (!detail) return;
      applyRuntime(detail, true);
      if (!EFFECT_KEYS.some((key) => has(detail, key))) return;

      const effects = normalizeAudioEffects({ ...confirmedEffects.current, ...detail });
      const id = Number(detail.__microphoneEffectSequence) || 0;
      if (!id && pending.current) return;
      confirmedEffects.current = effects;
      if (!id || id === sequence.current) setMicrophoneEffects(effects);
    };

    globalThis.addEventListener?.(AUDIO_SETTINGS_CHANGED_EVENT, sync);
    return () => globalThis.removeEventListener?.(AUDIO_SETTINGS_CHANGED_EVENT, sync);
  }, [applyRuntime]);

  useEffect(() => {
    if (!audioSettings) return;
    applyRuntime(audioSettings);
    if (pending.current) return;

    const effects = normalizeAudioEffects(audioSettings);
    confirmedEffects.current = effects;
    setMicrophoneEffects(effects);
  }, [applyRuntime, audioSettings]);

  const updateMicrophone = useCallback(
    (patch) =>
      enqueue(async () => {
        try {
          const updated = await api.updateAudioSettings(patch);
          notify(updated);
          return updated;
        } catch (error) {
          if (mounted.current) reportError(error);
          return null;
        }
      }),
    [enqueue, mounted, reportError]
  );

  const updateMicrophoneEffects = useCallback(
    (patch) => {
      const id = ++sequence.current;
      pending.current += 1;
      setMicrophoneEffects((current) => normalizeAudioEffects({ ...current, ...patch }));

      return enqueue(async () => {
        try {
          const updated = await api.updateAudioSettings(patch);
          confirmedEffects.current = normalizeAudioEffects({ ...confirmedEffects.current, ...updated });
          notify({ ...updated, __microphoneEffectSequence: id });
          return updated;
        } catch (error) {
          if (mounted.current) {
            if (id === sequence.current) setMicrophoneEffects(confirmedEffects.current);
            reportError(error);
          }
          return null;
        } finally {
          pending.current = Math.max(0, pending.current - 1);
        }
      });
    },
    [enqueue, mounted, reportError]
  );

  const setMicrophoneVolume = useCallback(
    (volume) => setRuntime((current) => ({ ...current, volume })),
    []
  );
  const setDirectOutputDeviceId = useCallback(
    (outputDeviceId) => setRuntime((current) => ({ ...current, outputDeviceId })),
    []
  );
  const setMonitoringEnabled = useCallback(
    (monitoringEnabled) => setRuntime((current) => ({ ...current, monitoringEnabled: !!monitoringEnabled })),
    []
  );

  return {
    microphoneVolume: runtime.volume,
    setMicrophoneVolume,
    microphoneEffects,
    setMicrophoneEffects,
    audioDriver: runtime.audioDriver,
    directOutputDeviceId: runtime.outputDeviceId,
    setDirectOutputDeviceId,
    monitoringEnabled: runtime.monitoringEnabled,
    setMonitoringEnabled,
    monitorInputDeviceId,
    updateMicrophone,
    updateMicrophoneEffects
  };
}
