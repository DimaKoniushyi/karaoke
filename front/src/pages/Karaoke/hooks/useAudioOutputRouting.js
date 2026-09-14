import { useEffect } from "react";
import { findDriverOutputDevice, findMatchingBrowserOutput } from "../utils/audio-settings";

const safe = (task, onError) => Promise.resolve().then(task).catch((error) => onError?.(error));
const publishRoute = (deviceId = "", matched = true) =>
  globalThis.dispatchEvent?.(
    new CustomEvent("audio-output-route-changed", { detail: { deviceId, matched } })
  );

export default function useAudioOutputRouting({
  audioDriver,
  audioSettings,
  directOutputDeviceId,
  directOutputDevices,
  instrumentalRef,
  setDirectOutputDeviceId,
  updateMicrophone,
  videoRef,
  vocalsRef
}) {
  useEffect(() => {
    if (audioDriver !== "asio" || String(audioSettings?.output_device_id ?? "").trim()) return;
    const device = findDriverOutputDevice(directOutputDevices, audioSettings?.asio_driver_name);
    if (!device || String(directOutputDeviceId) === String(device.index)) return;

    let active = true;
    safe(async () => {
      const updated = await updateMicrophone({ output_device_id: device.index });
      // Guards against a stale response applying a device switch after the
      // driver/output selection has already moved on (e.g. the user left
      // ASIO mode, or picked a different output, while this was in flight).
      if (active && updated) setDirectOutputDeviceId(updated.output_device_id ?? device.index);
    });
    return () => {
      active = false;
    };
  }, [
    audioDriver,
    audioSettings?.asio_driver_name,
    audioSettings?.output_device_id,
    directOutputDeviceId,
    directOutputDevices,
    setDirectOutputDeviceId,
    updateMicrophone
  ]);

  useEffect(() => {
    let active = true;
    const route = (deviceId = "", matched = true) => {
      if (!active) return;
      publishRoute(deviceId, matched);
      for (const media of [instrumentalRef.current, vocalsRef.current, videoRef.current]) {
        if (typeof media?.setSinkId === "function") {
          safe(
            () => media.setSinkId(deviceId),
            // Previously swallowed entirely -- the selected speakers could
            // silently stay on the system default with zero signal that the
            // switch itself failed, not just that no match was found (below).
            (error) =>
              // eslint-disable-next-line no-console
              console.warn("Could not route playback to the selected output device", error)
          );
        }
      }
    };

    const selected = (Array.isArray(directOutputDevices) ? directOutputDevices : []).find(
      ({ index }) => String(index) === String(directOutputDeviceId)
    );
    const devices = globalThis.navigator?.mediaDevices;

    if (directOutputDeviceId == null || directOutputDeviceId === "") {
      route("", true);
    } else if (!selected) {
      route("", false);
    } else if (typeof devices?.enumerateDevices === "function") {
      devices
        .enumerateDevices()
        .then((entries) => {
          const match = findMatchingBrowserOutput(entries, selected)?.deviceId || "";
          // "" (no match) means the requested device silently falls back to
          // the system default -- previously indistinguishable from a
          // deliberate "use the system default" request.
          route(match, Boolean(match));
        })
        .catch(() => route("", false));
    } else {
      route("", false);
    }

    return () => {
      active = false;
    };
  }, [directOutputDeviceId, directOutputDevices, instrumentalRef, videoRef, vocalsRef]);
}
