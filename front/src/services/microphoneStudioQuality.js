import { closeAudioContext, closeAudioContextQuietly } from "../utils/audio-context";
import { AUDIO_SETTINGS_CHANGED_EVENT } from "../utils/audioSettingsEvents";
import { clampWithFallback } from "../utils/math";
import { connectMicrophoneChannelStrip } from "./microphoneChannelStrip";
import { connectVoiceEffects } from "./voiceEffects";

const stop = (stream) => stream?.getTracks?.().forEach((track) => track.stop());
const disconnect = (node) => {
  try {
    node?.disconnect?.();
  } catch {
    // A source can already be detached by the browser.
  }
};

function rawGraph(stream) {
  return {
    stream,
    effectsStream: stream,
    rawStream: stream,
    getStream: () => stream,
    getEffectsStream: () => stream,
    setEffects: () => false,
    setMonitoring: () => false,
    close: async () => stop(stream)
  };
}

export function createStudioMicrophoneGraph(rawStream, options = {}) {
  const AudioContext = globalThis.AudioContext ?? globalThis.webkitAudioContext;
  if (!AudioContext?.prototype || !rawStream) return rawGraph(rawStream);
  let context;
  let source;
  let input = rawStream;
  let syncHandler = null;
  try {
    // Let the browser use the endpoint's native rate. 41 kHz is not a standard
    // hardware rate and makes some Windows devices resample or fail to open.
    // A numeric hint asks Chromium for the smallest practical realtime
    // quantum instead of its broader generic "interactive" profile. The
    // browser may clamp this for a particular Windows device, so this remains
    // a safe preference rather than a hard hardware requirement.
    context = new AudioContext({ latencyHint: 0 });
    source = context.createMediaStreamSource(input);
    const destination = context.createMediaStreamDestination();
    const effectsDestination = context.createMediaStreamDestination();
    const processedBus = context.createGain();
    const finalOutput = context.createGain();
    const effectsMix = context.createGain();
    const strip = connectMicrophoneChannelStrip(context, source, processedBus, {
      noiseSuppression: options.noiseSuppression ?? 0.35,
      realtime: true
    });
    processedBus.connect(finalOutput);
    finalOutput.gain.value = clampWithFallback(options.volume ?? 1, 0, 2, 0);
    finalOutput.connect(destination);
    // Keep an immediate dry path in the effects stream. Reverb/echo are mixed
    // alongside it, never inserted before it, so enabling studio effects does
    // not move the singer's consonants later in time.
    finalOutput.connect(effectsMix);
    effectsMix.connect(effectsDestination);
    let pitchNode = null;
    let pitchModulePromise = null;
    let pitchVersion = 0;
    const setPitchShift = async (value) => {
      const octave = Math.max(-1, Math.min(1, Number(value) || 0));
      const version = ++pitchVersion;
      if (Math.abs(octave) < 0.001) {
        disconnect(processedBus);
        disconnect(pitchNode);
        pitchNode = null;
        processedBus.connect(finalOutput);
        return 0;
      }
      if (pitchNode) {
        const parameter = pitchNode.parameters?.get?.("ratio");
        const ratio = 2 ** octave;
        if (typeof parameter?.setTargetAtTime === "function")
          parameter.setTargetAtTime(ratio, context.currentTime, 0.012);
        else if (typeof parameter?.setValueAtTime === "function")
          parameter.setValueAtTime(ratio, context.currentTime);
        else if (parameter) parameter.value = ratio;
        return octave;
      }
      if (!context.audioWorklet || typeof globalThis.AudioWorkletNode !== "function") return 0;
      pitchModulePromise ??= context.audioWorklet.addModule(
        new URL("./pitchShiftProcessor.js", import.meta.url)
      );
      await pitchModulePromise;
      if (version !== pitchVersion || context.state === "closed") return octave;
      const nextNode = new globalThis.AudioWorkletNode(context, "advoice-pitch-shift", {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1]
      });
      const ratio = 2 ** octave;
      const parameter = nextNode.parameters?.get?.("ratio");
      if (typeof parameter?.setValueAtTime === "function")
        parameter.setValueAtTime(ratio, context.currentTime);
      else if (parameter) parameter.value = ratio;
      disconnect(processedBus);
      disconnect(pitchNode);
      pitchNode = nextNode;
      processedBus.connect(pitchNode);
      pitchNode.connect(finalOutput);
      return octave;
    };
    setPitchShift(options.octave).catch(() => {});
    let effectGraph = null;
    let effectsKey = null;
    const clearEffects = () => {
      effectGraph?.close();
      effectGraph = null;
      effectsKey = null;
    };
    const setEffects = (effects = {}) => {
      // Volume is a plain gain value, applied unconditionally -- it never
      // needs the delay/convolver graph below rebuilt. reverb/echo/delay are
      // the only inputs that actually change that graph's shape, so a pure
      // volume change (or a monitoring toggle re-applying the same,
      // already-active effects -- see setMonitoring) now costs one gain
      // assignment instead of tearing down and reconnecting every node.
      finalOutput.gain.value = clampWithFallback(effects.volume ?? finalOutput.gain.value ?? 1, 0, 2, 0);
      const key = `${effects.reverb ?? 0}|${effects.echo ?? 0}|${effects.delay ?? 0}`;
      if (key === effectsKey && effectGraph) return true;
      clearEffects();
      effectsKey = key;
      effectGraph = connectVoiceEffects(context, finalOutput, effectsMix, effects);
      return true;
    };
    setEffects(options);
    let monitor = null;
    const stopMonitoring = () => {
      if (!monitor) return false;
      try {
        effectsMix.disconnect(monitor.gain);
      } catch {
        // The monitor can already be detached while the context is closing.
      }
      monitor.nodes.forEach(disconnect);
      monitor = null;
      return false;
    };
    const setMonitoring = (enabled, effects = {}) => {
      stopMonitoring();
      if (!enabled) return false;
      const gain = context.createGain();
      const nodes = [gain];
      gain.gain.value = 1;
      setEffects(effects);
      effectsMix.connect(gain);
      gain.connect(context.destination);
      monitor = { gain, nodes };
      return true;
    };
    const sync = ({ detail }) => {
      if (detail?.noise_suppression != null) strip.setNoiseSuppression(detail.noise_suppression);
      if (detail?.octave != null) setPitchShift(detail.octave).catch(() => {});
      if (["volume", "reverb", "echo", "delay"].some((name) => detail?.[name] != null))
        setEffects(detail);
    };
    syncHandler = sync;
    globalThis.addEventListener?.(AUDIO_SETTINGS_CHANGED_EVENT, sync);
    destination.stream.getAudioTracks?.().forEach((track) => {
      track.contentHint = "music";
    });
    effectsDestination.stream.getAudioTracks?.().forEach((track) => {
      track.contentHint = "music";
    });
    context.resume?.().catch(() => {});
    return {
      stream: destination.stream,
      effectsStream: effectsDestination.stream,
      rawStream,
      context,
      setNoiseSuppression: strip.setNoiseSuppression,
      setPitchShift,
      setEffects,
      setMonitoring,
      getStream: ({ disabledEffects = false, effectsEnabled = false } = {}) =>
        disabledEffects ? input : effectsEnabled ? effectsDestination.stream : destination.stream,
      getEffectsStream: () => effectsDestination.stream,
      async replaceInput(stream) {
        const next = context.createMediaStreamSource(stream);
        next.connect(strip.highpass);
        disconnect(source);
        stop(input);
        input = stream;
        source = next;
      },
      async close() {
        globalThis.removeEventListener?.(AUDIO_SETTINGS_CHANGED_EVENT, sync);
        stopMonitoring();
        clearEffects();
        strip.close?.();
        pitchVersion += 1;
        disconnect(pitchNode);
        disconnect(processedBus);
        disconnect(finalOutput);
        disconnect(effectsMix);
        disconnect(source);
        stop(input);
        stop(destination.stream);
        stop(effectsDestination.stream);
        if (context.state !== "closed") await closeAudioContext(context);
      }
    };
  } catch (error) {
    if (syncHandler) globalThis.removeEventListener?.(AUDIO_SETTINGS_CHANGED_EVENT, syncHandler);
    stop(rawStream);
    closeAudioContextQuietly(context);
    throw error;
  }
}
