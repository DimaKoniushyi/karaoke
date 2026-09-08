export {
  advanceMusicLighting,
  lightingColor,
  musicLightingColor,
  musicLightingZones,
  sameLightingPalette
} from "../utils/lightingColor";

const sources = new Map();
export function registerLightingSource(name, read) {
  sources.set(name, read);
  return () => {
    if (sources.get(name) === read) sources.delete(name);
  };
}
export function readLightingMusic() {
  for (const name of ["karaoke", "radio"]) {
    try {
      const sample = sources.get(name)?.();
      if (sample?.active) return sample;
    } catch {
      /* Visuals must not interrupt playback. */
    }
  }
  return { active: false, level: 0 };
}

// Read a copy of already playing media. Never re-route the original element,
// open the microphone, or connect audible analysis output.
export function observeLightingMedia(media, register = registerLightingSource) {
  const AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!media?.captureStream || !AudioContext) return () => {};
  let context,
    stream,
    source,
    analyser,
    samples,
    silent,
    disposed = false;
  const unregister = register("karaoke", () => {
    if (!analyser || media.paused || media.ended || context?.state !== "running")
      return { active: false, level: 0 };
    analyser.getByteFrequencyData(samples);
    let sum = 0;
    const end = Math.min(
      samples.length,
      Math.max(2, Math.ceil((250 * analyser.fftSize) / context.sampleRate))
    );
    for (let i = 1; i < end; i++) sum += samples[i] / 255;
    const bass = sum / Math.max(1, end - 1);
    return { active: true, level: Math.min(1, bass ** 1.35 * 1.45) };
  });
  const attach = () => {
    if (disposed || source || !stream?.getAudioTracks().length) return;
    try {
      context = new AudioContext();
      source = context.createMediaStreamSource(stream);
      analyser = context.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.72;
      samples = new Uint8Array(analyser.frequencyBinCount);
      silent = context.createGain();
      silent.gain.value = 0;
      source.connect(analyser);
      analyser.connect(silent);
      silent.connect(context.destination);
      context.resume().catch(() => {});
    } catch {
      cleanupAudio();
    }
  };
  const cleanupAudio = () => {
    source?.disconnect();
    analyser?.disconnect();
    silent?.disconnect();
    context?.close().catch(() => {});
    source = null;
    analyser = null;
    context = null;
  };
  const start = () => {
    if (disposed) return;
    try {
      if (!stream) {
        stream = media.captureStream();
        stream.addEventListener("addtrack", attach);
      }
      attach();
      context?.resume().catch(() => {});
    } catch {
      /* Unsupported/cross-origin capture: leave audio unchanged. */
    }
  };
  media.addEventListener("playing", start);
  if (!media.paused) start();
  return () => {
    disposed = true;
    unregister();
    media.removeEventListener("playing", start);
    stream?.removeEventListener("addtrack", attach);
    stream?.getTracks().forEach((track) => track.stop());
    cleanupAudio();
  };
}
