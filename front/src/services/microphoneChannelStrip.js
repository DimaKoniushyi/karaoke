import { clamp01Safe as clamp01 } from "../utils/math";
import { createLevelMeter } from "./levelMeter";

const CURVES = { limiter: 1024 };

function curve(length, transform) {
  return Float32Array.from({ length }, (_, index) => transform((index / (length - 1)) * 2 - 1));
}

export function buildSoftLimiterCurve(drive = 1.03) {
  const limit = Math.tanh(drive);
  return curve(CURVES.limiter, (sample) => Math.tanh(sample * drive) / limit);
}

const assign = (node, values) => {
  for (const [key, value] of Object.entries(values)) node[key].value = value;
  return node;
};

export function connectMicrophoneChannelStrip(
  context,
  source,
  destination,
  { noiseSuppression = 0.35, realtime = false } = {}
) {
  const highpass = assign(context.createBiquadFilter(), { frequency: 70 });
  highpass.type = "highpass";
  const meter = createLevelMeter(context, { smoothingTimeConstant: 0.45 });
  const analyser = meter?.analyser;
  // Starts as a plain, always-open GainNode -- identical to "no gate" -- and
  // is swapped in place for a real AudioWorkletNode-based gate below once
  // its module loads (mirrors microphoneStudioQuality.js's setPitchShift,
  // which loads its own worklet the same way). Never blocks synchronous
  // graph construction, and an environment without AudioWorklet support
  // keeps working via the setInterval-driven fallback further down.
  const noiseGate = assign(context.createGain(), { gain: 1 });
  // Boosting presence before the compressor makes the compressor's envelope
  // detector react harder to exactly the frequencies (~2-8kHz) where hiss and
  // sibilance live, and the following limiter then has to soft-clip that
  // already-brightened, already-compressed signal -- a combination reported
  // to sound harsh/hissy to listeners on the receiving end. Trading some
  // presence/"clarity" for a cleaner signal here.
  const presence = assign(context.createBiquadFilter(), { frequency: 2200, gain: 1.2 });
  presence.type = "highshelf";
  const compressor = assign(context.createDynamicsCompressor(), {
    threshold: -16,
    knee: 6,
    ratio: 3,
    attack: 0.01,
    release: 0.15
  });
  const makeup = assign(context.createGain(), { gain: 1.04 });
  const limiter = context.createWaveShaper();
  Object.assign(limiter, {
    curve: buildSoftLimiterCurve(),
    // Oversampling is useful for an offline/final recording, but it adds
    // filtering delay to the live duet path. The realtime graph keeps the
    // limiter and all user-facing processing without that extra buffer.
    oversample: realtime ? "none" : "2x"
  });
  // DynamicsCompressorNode has a fixed look-ahead delay. In a live room the
  // soft limiter already protects peaks, so bypass only that redundant node;
  // high-pass, noise gate, tone, gain and limiter remain active.
  const chain = realtime
    ? [highpass, analyser, noiseGate, presence, makeup, limiter, destination]
    : [highpass, analyser, noiseGate, presence, compressor, makeup, limiter, destination];
  const linked = chain.filter(Boolean);
  linked.reduce((node, next) => node.connect(next), source);
  const gateIndex = linked.indexOf(noiseGate);
  const gatePrev = gateIndex > 0 ? linked[gateIndex - 1] : source;
  const gateNext = linked[gateIndex + 1];

  let suppression = clamp01(noiseSuppression);
  let lastVoiceAt = 0;
  let timer = null;
  let gateWorklet = null;
  let closed = false;
  const canUseWorklet =
    Boolean(context.audioWorklet) && typeof globalThis.AudioWorkletNode === "function";

  if (canUseWorklet) {
    context.audioWorklet
      .addModule(new URL("./noiseGateProcessor.js", import.meta.url))
      .then(() => {
        if (closed) return;
        const worklet = new globalThis.AudioWorkletNode(context, "advoice-noise-gate", {
          numberOfInputs: 1,
          numberOfOutputs: 1,
          outputChannelCount: [1]
        });
        worklet.port.postMessage({ suppression });
        try {
          gatePrev.disconnect(noiseGate);
        } catch {
          // Already detached (e.g. close() ran just before this resolved).
        }
        if (gateNext) {
          try {
            noiseGate.disconnect(gateNext);
          } catch {
            // Already detached.
          }
          gatePrev.connect(worklet);
          worklet.connect(gateNext);
        }
        gateWorklet = worklet;
      })
      .catch(() => {
        // No worklet support in practice despite the feature check (e.g. a
        // restrictive CSP) -- the setInterval fallback below keeps working.
      });
  }
  if (analyser && !canUseWorklet) {
    // Only when a true realtime gate could not be set up at all -- the
    // worklet above is a strict upgrade over this main-thread timer, which
    // can run late under renderer load (heavy React work, GC pauses) and
    // make the gate open/close late or audibly "pump".
    const updateGate = () => {
      const rms = meter.read();
      const now = Date.now();
      const threshold = 0.0035 + suppression * 0.008;
      if (rms >= threshold) lastVoiceAt = now;
      const held = now - lastVoiceAt < 140;
      const openness = Math.min(1, rms / threshold);
      const target =
        suppression === 0 || held ? 1 : Math.max(0.12, 1 - suppression * 0.88 * (1 - openness));
      if (typeof noiseGate.gain.setTargetAtTime === "function") {
        noiseGate.gain.setTargetAtTime(
          target,
          context.currentTime,
          target > noiseGate.gain.value ? 0.008 : 0.12
        );
      } else noiseGate.gain.value = target;
    };
    timer = globalThis.setInterval(updateGate, 24);
    timer?.unref?.();
  }
  return {
    highpass,
    analyser,
    noiseGate,
    presence,
    compressor,
    makeup,
    limiter,
    setNoiseSuppression: (value) => {
      suppression = clamp01(value);
      gateWorklet?.port.postMessage({ suppression });
    },
    close: () => {
      closed = true;
      if (timer) globalThis.clearInterval(timer);
      try {
        gateWorklet?.disconnect();
      } catch {
        // Already detached.
      }
    }
  };
}
