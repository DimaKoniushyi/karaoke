// Builds a microphoneGraph-shaped object (same shape as
// createStudioMicrophoneGraph's return value: stream/effectsStream/
// rawStream/context/setMonitoring/close/...) from the Python monitor's audio
// relay instead of a local getUserMedia + Web Audio DSP chain. Used by
// OnlineVoiceMesh so the room can reuse the *same* gate/compressor/
// pitch-shift/reverb-echo-delay processing the solo monitor already does,
// instead of a second, independent JS implementation of the same DSP.
//
// No node here ever connects to context.destination -- this graph must never
// play audio locally. Self-monitoring in the singer's own headphones already
// happens on the Python side (the same monitor process that feeds this
// relay also still owns the one real hardware output), so a second local
// playback path would double it (see the room_mode comment in
// backend/app/routers/recording.py).
import { API_BASE_URL } from "../runtime-config";
import { closeAudioContext } from "../utils/audio-context";
import { apiToken } from "../utils/platform";

const RELAY_PATH = "/audio/direct-monitor/relay";
const STREAM_WET = 1;
// Matches audio_relay_protocol.py's _HEADER = struct.Struct("<IfI"): three
// 4-byte fields, so the PCM payload starts at a 4-byte-aligned offset and a
// Float32Array can view it directly instead of always being copied out with
// .slice() first just to satisfy TypedArray alignment.
const HEADER_BYTES = 12; // uint32 stream_id + float32 sample_rate + uint32 sample_count
const DEFAULT_CONNECT_TIMEOUT_MS = 2000;
const MAX_PENDING_FRAMES = 8;

function relayWebSocketUrl() {
  const token = apiToken();
  const wsBase = String(API_BASE_URL).replace(/^http/i, "ws");
  return `${wsBase}${RELAY_PATH}${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}

function parseFrame(buffer) {
  const view = new DataView(buffer);
  const streamId = view.getUint32(0, true);
  const sampleRate = view.getFloat32(4, true);
  const sampleCount = view.getUint32(8, true);
  // A direct view, not a copy: the buffer is 4-byte aligned at HEADER_BYTES
  // (see the comment above), and this ArrayBuffer is never reused for
  // another message (it's the argument WebSocket handed this one onmessage
  // call), so there is no aliasing risk in holding a live view over it.
  const samples = new Float32Array(buffer, HEADER_BYTES, sampleCount);
  return { streamId, sampleRate, samples };
}

function connectRelaySocket(timeoutMs) {
  return new Promise((resolve, reject) => {
    if (typeof globalThis.WebSocket !== "function") {
      reject(new Error("WebSocket is not supported in this environment"));
      return;
    }
    let socket;
    try {
      socket = new globalThis.WebSocket(relayWebSocketUrl());
    } catch (error) {
      reject(error);
      return;
    }
    socket.binaryType = "arraybuffer";
    let settled = false;
    const clear = () => {
      clearTimeout(timer);
      socket.onopen = null;
      socket.onmessage = null;
      socket.onclose = null;
      socket.onerror = null;
    };
    // The server accepts the connection even when it has nothing to relay
    // (see _NO_RELAY_CLOSE_CODE in app/routers/audio_relay.py), so success is
    // only declared once real audio actually arrives -- not merely on open.
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      clear();
      socket.close();
      reject(new Error("Timed out connecting to the microphone relay"));
    }, timeoutMs);
    socket.onmessage = (event) => {
      if (settled || !(event.data instanceof ArrayBuffer)) return;
      settled = true;
      clear();
      resolve({ socket, firstFrame: parseFrame(event.data) });
    };
    socket.onclose = (event) => {
      if (settled) return;
      settled = true;
      clear();
      reject(new Error(`Microphone relay unavailable (code ${event.code})`));
    };
    socket.onerror = () => {
      if (settled) return;
      settled = true;
      clear();
      reject(new Error("Microphone relay connection failed"));
    };
  });
}

async function makeVoice(context, sourceRate) {
  const node = new globalThis.AudioWorkletNode(context, "advoice-relay-playback", {
    numberOfInputs: 0,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    // Lets the processor resample correctly even when the browser could not
    // actually honor the sampleRate this context was requested with below
    // (previously undetectable here -- playback just ran at a slightly
    // wrong pitch/speed with no resampling at all).
    processorOptions: { sourceRate }
  });
  const destination = context.createMediaStreamDestination();
  node.connect(destination);
  destination.stream.getAudioTracks().forEach((track) => {
    track.contentHint = "music";
  });
  return { node, destination };
}

export async function createRelayVoiceGraph({ connectTimeoutMs = DEFAULT_CONNECT_TIMEOUT_MS } = {}) {
  const AudioContextCtor = globalThis.AudioContext ?? globalThis.webkitAudioContext;
  if (!AudioContextCtor?.prototype || typeof globalThis.AudioWorkletNode !== "function") {
    throw new Error("Web Audio is not supported in this environment");
  }

  const { socket, firstFrame } = await connectRelaySocket(connectTimeoutMs);
  let context;
  try {
    // connectRelaySocket() clears socket.onmessage once it resolves, and the
    // relay keeps sending frames continuously (~every 5ms) the whole time --
    // without a handler here, anything that arrives during the async work
    // below (module load + two AudioWorkletNode constructions) would be
    // silently dropped, since a WebSocket does not buffer for a caller that
    // isn't listening. Queue raw frames here and replay them once dry/wet
    // are actually ready to receive, instead of losing that startup window.
    const pending = [];
    socket.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) return;
      pending.push(event.data);
      if (pending.length > MAX_PENDING_FRAMES) pending.shift();
    };

    // Requesting the source rate avoids any resampling work when it matches
    // the browser's own hardware rate (commonly true -- both usually settle
    // on 48kHz). A browser that cannot honor this falls back to its own
    // native rate; relayPlaybackProcessor.js resamples from the real source
    // rate (passed as processorOptions.sourceRate below) to whatever rate
    // the context actually ended up at, so playback pitch/speed stays
    // correct either way instead of only in the common case.
    context = new AudioContextCtor({ sampleRate: firstFrame.sampleRate || undefined });
    await context.audioWorklet.addModule(new URL("./relayPlaybackProcessor.js", import.meta.url));

    const dry = await makeVoice(context, firstFrame.sampleRate);
    const wet = await makeVoice(context, firstFrame.sampleRate);
    let closed = false;
    let unavailableCallback = null;
    const deliver = (frame) => {
      const target = frame.streamId === STREAM_WET ? wet : dry;
      target.node.port.postMessage(frame.samples, [frame.samples.buffer]);
    };
    deliver(firstFrame);
    for (const buffer of pending) {
      try {
        deliver(parseFrame(buffer));
      } catch {
        // A malformed frame is dropped rather than tearing down the call.
      }
    }

    socket.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) return;
      try {
        deliver(parseFrame(event.data));
      } catch {
        // A malformed frame is dropped rather than tearing down the call.
      }
    };
    const handleUnavailable = () => {
      if (closed) return;
      unavailableCallback?.();
    };
    socket.onclose = handleUnavailable;
    socket.onerror = handleUnavailable;

    return {
      stream: dry.destination.stream,
      effectsStream: wet.destination.stream,
      rawStream: dry.destination.stream,
      context,
      setNoiseSuppression: () => {},
      setPitchShift: async () => 0,
      setEffects: () => false,
      // Self-monitoring already runs on the Python side whenever monitoring
      // is enabled, independent of this toggle -- this graph never plays
      // anything locally (see the module docstring above), so the room's
      // "hear yourself" control is intentionally inert here.
      setMonitoring: () => false,
      getStream: ({ effectsEnabled = false } = {}) =>
        effectsEnabled ? wet.destination.stream : dry.destination.stream,
      getEffectsStream: () => wet.destination.stream,
      // OnlineVoiceMesh subscribes to be told when the relay drops mid-call
      // (network blip, monitor stopped) so it can fall back to the local JS
      // DSP graph instead of leaving peers with dead audio.
      onUnavailable(callback) {
        unavailableCallback = callback;
      },
      async close() {
        closed = true;
        socket.onmessage = null;
        socket.onclose = null;
        socket.onerror = null;
        try {
          socket.close();
        } catch {
          // Already closing/closed.
        }
        [dry, wet].forEach(({ node, destination }) => {
          try {
            node.disconnect();
          } catch {
            // Already detached.
          }
          destination.stream.getTracks().forEach((track) => track.stop());
        });
        if (context.state !== "closed") await closeAudioContext(context);
      }
    };
  } catch (error) {
    try {
      socket.close();
    } catch {
      // Already closing/closed.
    }
    if (context) await closeAudioContext(context).catch(() => {});
    throw error;
  }
}
