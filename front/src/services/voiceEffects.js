import { clampWithFallback } from "../utils/math";

const clamp = (value) => clampWithFallback(value, 0, 1, 0);

// connectVoiceEffects is rebuilt on every settings change synced into this
// graph (see microphoneStudioQuality.js's sync()) -- even a plain volume
// change used to regenerate the reverb impulse response from scratch (up to
// ~150,000 Math.random() calls at a long tail/high sample rate) purely
// because it happened to share a code path with reverb/echo/delay changes.
// Cached per (AudioContext, reverb amount) instead; bucketed to a few
// hundred steps so the slider still sounds continuous while dragging without
// generating a fresh buffer on every intermediate value.
const impulseCache = new WeakMap();
function reverbImpulse(context, reverb) {
  let perContext = impulseCache.get(context);
  if (!perContext) {
    perContext = new Map();
    impulseCache.set(context, perContext);
  }
  const key = Math.round(reverb * 200);
  const cached = perContext.get(key);
  if (cached) return cached;
  const frames = Math.floor(context.sampleRate * (0.35 + reverb * 1.15));
  const impulse = context.createBuffer(2, frames, context.sampleRate);
  for (let channel = 0; channel < impulse.numberOfChannels; channel += 1) {
    const data = impulse.getChannelData(channel);
    for (let index = 0; index < frames; index += 1)
      data[index] = (Math.random() * 2 - 1) * (1 - index / frames) ** (1.5 + reverb * 2);
  }
  perContext.set(key, impulse);
  return impulse;
}

// Adds wet branches to an existing graph; the caller retains the dry path,
// output gain and AudioContext lifecycle. No extra processing on dry audio.
export function connectVoiceEffects(context, source, destination, effects = {}) {
  const connections = [],
    nodes = [];
  const connect = (from, to) => {
    from.connect(to);
    connections.push([from, to]);
  };
  const close = () => {
    for (const [from, to] of connections.splice(0)) {
      try {
        from.disconnect(to);
      } catch {
        /* Already detached. */
      }
    }
    for (const node of nodes.splice(0)) {
      try {
        node.disconnect();
      } catch {
        /* Already detached. */
      }
    }
  };
  try {
    const echo = clamp(effects.echo),
      delayAmount = clamp(effects.delay);
    if (echo || delayAmount) {
      const delay = context.createDelay(1);
      const feedback = context.createGain();
      const wet = context.createGain();
      nodes.push(delay, feedback, wet);
      delay.delayTime.value = 0.06 + delayAmount * 0.34;
      feedback.gain.value = Math.min(0.72, echo * 0.55 + delayAmount * 0.3);
      wet.gain.value = Math.min(0.65, echo * 0.46 + delayAmount * 0.24);
      connect(source, delay);
      connect(delay, feedback);
      connect(feedback, delay);
      connect(delay, wet);
      connect(wet, destination);
    }
    const reverb = clamp(effects.reverb);
    if (reverb) {
      const convolver = context.createConvolver();
      const wet = context.createGain();
      nodes.push(convolver, wet);
      convolver.buffer = reverbImpulse(context, reverb);
      wet.gain.value = Math.min(0.58, reverb * 0.48);
      connect(source, convolver);
      connect(convolver, wet);
      connect(wet, destination);
    }
    return { close };
  } catch (error) {
    close();
    throw error;
  }
}
