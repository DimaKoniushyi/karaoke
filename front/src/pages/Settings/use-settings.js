import { useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import { useAppDialog } from "../../contexts/AppDialog";
import { useRadio } from "../../contexts/radio";
import useAppSettings from "../../hooks/useAppSettings";
import { usePolling } from "../../hooks/usePolling";
import { translateSaved as tr } from "../../i18n/runtime";
import { POLLING_INTERVALS as POLL } from "../../runtime-config";
import { createInputDeviceOptions, createOutputDeviceOptions } from "../../utils/audio-devices";
import { AUDIO_SETTINGS_CHANGED_EVENT } from "../../utils/audioSettingsEvents";
import { getErrorMessage } from "../../utils/errors";
import { getLightingStatus } from "../../utils/platform";
import { applyTheme } from "../../utils/theme";
import { findMatchingBrowserOutput } from "../Karaoke/utils/audio-settings";

const MME_SENTINEL = "mme";
const emit = (detail) => dispatchEvent(new CustomEvent(AUDIO_SETTINGS_CHANGED_EVENT, { detail }));
const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const useOpenPoll = (open, fn, interval, fallback) =>
  usePolling(() => (open ? fn() : Promise.resolve(fallback)), open ? interval : 0, [open]);

function useQueue() {
  const queue = useRef(Promise.resolve());
  const latest = useRef(new Map());
  return (key, task) => {
    const token = Symbol(key);
    latest.current.set(key, token);
    return (queue.current = queue.current
      .catch(() => {})
      .then(() => task(() => latest.current.get(key) === token)));
  };
}

// Settings only knows the user's chosen output as a native device index
// (output_device_id); the browser's own device list uses a completely
// different id space, so the two must be matched by device name -- the same
// approach the Karaoke screen already uses for its own output routing (see
// useAudioOutputRouting.js).
async function resolveOutputSinkId(outputDeviceId, outputDevices) {
  const selected = (Array.isArray(outputDevices) ? outputDevices : []).find(
    ({ index }) => String(index) === String(outputDeviceId)
  );
  const devices = globalThis.navigator?.mediaDevices;
  if (outputDeviceId == null || outputDeviceId === "" || !selected || typeof devices?.enumerateDevices !== "function") {
    return "";
  }
  try {
    const entries = await devices.enumerateDevices();
    return findMatchingBrowserOutput(entries, selected)?.deviceId || "";
  } catch {
    return "";
  }
}

async function testSpeaker(sinkId) {
  const Audio = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!Audio) return false;

  const context = new Audio({ latencyHint: "interactive" });
  // Without this, the test tone always plays on the system default output
  // regardless of which device is actually selected here -- silently
  // "testing" the wrong speakers whenever they differ.
  if (sinkId && typeof context.setSinkId === "function") {
    await context.setSinkId(sinkId).catch(() => {});
  }
  const tone = context.createOscillator();
  const gain = context.createGain();

  gain.gain.setValueAtTime(0.12, context.currentTime);
  tone.frequency.setValueAtTime(523.25, context.currentTime);
  tone.connect(gain).connect(context.destination);
  tone.start();
  tone.stop(context.currentTime + 0.7);

  try {
    await wait(800);
  } finally {
    await context.close();
  }
  return true;
}

export const signalLevel = (signal) => {
  const db = Number(signal?.rms_db ?? signal?.rms_dbfs);
  return Number.isFinite(db) ? Math.min(100, Math.max(0, ((db + 60) / 60) * 100)) : 0;
};

function useAppActions() {
  const { settings: form, error, updateSettings } = useAppSettings();
  const queue = useQueue();
  return {
    form,
    error,
    replace: updateSettings,
    change: (name, value) => name === "theme" && applyTheme(value),
    save: (name, value) =>
      queue(name, async (latest) => {
        const previous = form?.[name];
        try {
          const saved = await api.updateAppSettings({ [name]: value ?? null });
          if (latest()) updateSettings((state) => ({ ...state, [name]: saved[name] }));
        } catch (error) {
          if (latest() && name === "theme") applyTheme(previous);
          throw error;
        }
      })
  };
}

function useAudio(open) {
  const { alert } = useAppDialog();
  const queue = useQueue();
  const settings = useOpenPoll(open, api.getAudioSettings, POLL.settings, null);
  const inputs = useOpenPoll(open, api.listAudioDevices, POLL.devices, []);
  const outputs = useOpenPoll(open, api.listAudioOutputDevices, POLL.devices, []);
  const monitorStatus = useOpenPoll(open, api.getDirectMonitorStatus, 750, null);
  // The ASIO driver's own control panel can change the buffer out from
  // under a running monitor (e.g. ASIO4ALL/an interface's mixer app); the
  // backend detects that, restarts, and persists the size it actually
  // negotiated (see audio_service._persist_negotiated_buffer_size). The
  // fast-polled monitor status reflects that new size within ~1s; the
  // buffer_size dropdown otherwise only follows the much slower settings
  // poll, so react to that change here instead of leaving it stale for up
  // to POLL.settings.
  const negotiatedBlocksizeRef = useRef();
  useEffect(() => {
    const blocksize = monitorStatus.data?.blocksize;
    if (blocksize == null) return;
    if (
      negotiatedBlocksizeRef.current !== undefined &&
      negotiatedBlocksizeRef.current !== blocksize
    ) {
      settings.refresh();
    }
    negotiatedBlocksizeRef.current = blocksize;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [monitorStatus.data?.blocksize]);
  const [local, setLocal] = useState({});
  const [busy, setBusy] = useState(false);
  const values = { ...settings.data, ...local };
  // The level meter only ever renders while monitoring is on (see `level`
  // below) -- polling this while it's off used to open the microphone for a
  // real sd.rec()/sd.wait() capture every cycle purely to throw the result
  // away as a hardcoded 0.
  const signal = useOpenPoll(open && !!values.monitoring_enabled, api.getSignalQuality, POLL.realtimeSignal, null);
  const asio = useOpenPoll(open, api.listAsioDrivers, POLL.devices, []);
  const fail = (key, error) => alert(tr(key, { 0: getErrorMessage(error) }));
  const merge = (patch) => setLocal((state) => ({ ...state, ...patch }));
  const update = (name, value) => {
    const patch = { [name]: value };
    const previous = values[name];
    merge(patch);
    return queue(name, async (latest) => {
      try {
        const saved = await api.updateAudioSettings(patch);
        if (latest()) merge(saved);
        emit(saved);
        await settings.refresh();
      } catch (error) {
        if (latest()) merge({ [name]: previous });
        await fail("settings.couldNotSaveAudioSettings", error);
      }
    });
  };
  const monitor = async (retry = false) => {
    setBusy(true);
    try {
      const enabled = !!values.monitoring_enabled && !retry;
      const saved = await (enabled
        ? api.stopDirectMonitoring()
        : api.startDirectMonitoring({ disabledEffects: true }));

      merge({ monitoring_enabled: !enabled });
      emit(saved);
      await Promise.all([monitorStatus.refresh(), settings.refresh()]);
    } catch (error) {
      await fail("settings.couldNotChangeMonitoring", error);
    } finally {
      setBusy(false);
    }
  };
  const selectDriver = (name) =>
    queue("driver", async () => {
      try {
        // The dropdown's visible value is bound to asio_driver_name (see
        // audio.jsx), not audio_driver -- "auto" and "mme" would otherwise
        // both display as "" and be indistinguishable in the UI. MME_SENTINEL
        // is a reserved, non-empty stand-in stored in that field for "mme"
        // mode; nothing on the backend reads asio_driver_name unless
        // audio_driver is actually "asio", so this is safe there too.
        const driver = name === MME_SENTINEL ? "mme" : name ? "asio" : "auto";
        const saved = await api.updateAudioSettings({
          audio_driver: driver,
          asio_driver_name: driver === "asio" ? name : driver === "mme" ? MME_SENTINEL : ""
        });
        merge(saved);
        emit(saved);
        await settings.refresh();
      } catch (error) {
        await fail("settings.couldNotSaveAudioSettings", error);
      }
    });
  const speaker = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const sinkId = await resolveOutputSinkId(values.output_device_id, outputs.data);
      await testSpeaker(sinkId);
    } catch (error) {
      await fail("settings.failedToCheckSpeakers", error);
    } finally {
      setBusy(false);
    }
  };
  return {
    values,
    busy,
    update,
    monitor,
    speaker,
    selectDriver,
    monitorStatus: monitorStatus.data,
    monitorStatusError: monitorStatus.error,
    level: values.monitoring_enabled ? signalLevel(signal.data) : 0,
    options: {
      drivers: [
        { value: "", label: tr("settings.audio.wasapiMode.options.shared") },
        { value: MME_SENTINEL, label: "MME" },
        ...(asio.data ?? []).map(({ name }) => ({ value: name, label: `ASIO · ${name}` }))
      ],
      inputs: createInputDeviceOptions(inputs.data, values.input_device_id),
      outputs: createOutputDeviceOptions(
        outputs.data,
        values.output_device_id,
        values.audio_driver,
        tr("karaoke.systemDevice")
      )
    }
  };
}

export default function useSettings(open) {
  const lighting = useOpenPoll(open, getLightingStatus, 1500, null);
  return {
    app: useAppActions(),
    audio: useAudio(open),
    radio: useRadio(),
    lighting: lighting.data
  };
}
