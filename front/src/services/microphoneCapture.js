import { MICROPHONE_CAPTURE_CONSTRAINTS } from "../utils/microphone-capture-constraints";
import { createStudioMicrophoneGraph } from "./microphoneStudioQuality";

let active = null;
let queue = Promise.resolve();

// Every mutation of `active` -- acquiring, switching device, and releasing
// -- goes through this one FIFO queue. Previously only acquisition was
// serialized here; release() mutated `active` directly, so a release
// landing while a concurrent acquire was still mid-await (capturing a new
// stream or replacing the input device) could null out or hand back a
// graph a third caller was still relying on. Not reachable today (the only
// caller releases-then-acquires within one effect's synchronous prefix,
// which happens to order safely by microtask timing), but the `users`
// refcount is clearly designed for multiple concurrent holders, so this
// keeps the module correct once a second caller exists.
const enqueue = (task) => {
  const next = queue.catch(() => {}).then(task);
  queue = next.catch(() => {});
  return next;
};

const live = (stream) =>
  stream?.getAudioTracks?.().some(({ readyState }) => readyState === "live") === true;
const device = (value) => {
  const id = String(value ?? "").trim();
  return id && id !== "default" ? id : "";
};
const constraints = (id) => ({
  audio: {
    ...MICROPHONE_CAPTURE_CONSTRAINTS,
    channelCount: 1,
    // The room path feeds this into WebRTC, which encodes with Opus at a
    // fixed 48kHz internally regardless of what was captured -- requesting
    // 44.1kHz just to have it resampled again there added a conversion this
    // module's own WebAudio graph (which runs at its own AudioContext rate
    // either way) gets no benefit from.
    sampleRate: { ideal: 48_000 },
    sampleSize: { ideal: 24 },
    latency: { ideal: 0 },
    ...(id && { deviceId: { exact: id } })
  }
});

async function capture(id) {
  try {
    return { stream: await navigator.mediaDevices.getUserMedia(constraints(id)), deviceId: id, fellBackToDefault: false };
  } catch (error) {
    if (!id) throw error;
    return {
      stream: await navigator.mediaDevices.getUserMedia(constraints("")),
      deviceId: "",
      // The caller asked for a specific device (by id) and silently got a
      // different one instead -- surfaced on the acquired handle (see
      // acquireMicrophone's return value) rather than only visible as a
      // mismatch between the Settings UI and whatever is actually captured,
      // which especially confuses any manual latency comparison.
      fellBackToDefault: true
    };
  }
}

async function resolve(id) {
  // The graph's own output (graph.stream) is a MediaStreamDestination the
  // WebAudio graph itself keeps producing regardless of whether the
  // physical microphone is still connected -- checking it here would never
  // detect an unplugged device. rawStream is the actual getUserMedia track.
  if (!live(active?.graph?.rawStream)) {
    const next = await capture(id);
    active = {
      deviceId: next.deviceId,
      fellBackToDefault: next.fellBackToDefault,
      graph: createStudioMicrophoneGraph(next.stream),
      users: 0
    };
  } else if (id && active.deviceId !== id) {
    const next = await capture(id);
    await active.graph.replaceInput(next.stream);
    active.deviceId = next.deviceId;
    active.fellBackToDefault = next.fellBackToDefault;
  }
  return active;
}

// Chromium reliably ends a track when its physical device disappears
// mid-session (readyState -> "ended"), which the live() check above already
// recaptures
// on the next acquire -- but nothing previously reacted to a device
// connecting/disconnecting at all, so a stale/dead cached entry could sit
// unnoticed until something else happened to call acquireMicrophone again.
if (typeof navigator !== "undefined" && navigator.mediaDevices?.addEventListener) {
  navigator.mediaDevices.addEventListener("devicechange", () => {
    enqueue(async () => {
      if (active && !live(active.graph?.rawStream)) {
        const entry = active;
        active = null;
        await entry.graph.close();
      }
    });
  });
}

export async function acquireMicrophone(preferredDeviceId = "", { disabledEffects = false } = {}) {
  if (!navigator.mediaDevices?.getUserMedia) throw new Error("Microphone capture is unavailable");
  const entry = await enqueue(() => resolve(device(preferredDeviceId)));
  entry.users += 1;
  let released = false;
  return {
    stream:
      entry.graph.getStream?.({ disabledEffects }) ??
      (disabledEffects ? entry.graph.rawStream : entry.graph.stream),
    // The device actually captured, and whether it differs from what was
    // requested -- previously invisible to the caller, so the UI could keep
    // showing the originally selected device name while a different one was
    // silently in use underneath it.
    deviceId: entry.deviceId,
    fellBackToDefault: Boolean(entry.fellBackToDefault),
    setNoiseSuppression: entry.graph.setNoiseSuppression,
    // Local self-monitoring entirely inside the Web Audio graph -- no extra
    // OS-level audio stream, so it stays in sync with whatever else already
    // holds this same microphone (e.g. an online room capture) instead of
    // fighting it for exclusive device access.
    setMonitoring: entry.graph.setMonitoring,
    async release() {
      if (released) return;
      released = true;
      await enqueue(async () => {
        entry.users = Math.max(0, entry.users - 1);
        if (entry.users) return;
        // Once this entry's own last lease is gone it must always close --
        // not only while it's still the current `active`. resolve() replaces
        // `active` outright (not reusing this entry) when the device died,
        // so a caller still holding an older lease at that moment finds
        // `active !== entry` here; skipping the close in that case used to
        // leak this entry's whole Web Audio graph (AudioContext included)
        // forever, since nothing else ever references it again.
        if (active === entry) active = null;
        await entry.graph.close();
      });
    }
  };
}

export async function closeMicrophoneCapture() {
  await enqueue(async () => {
    const entry = active;
    active = null;
    if (entry) await entry.graph.close();
  });
}
