import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const STREAM_DRY = 0;
const STREAM_WET = 1;

// Matches audio_relay_protocol.py's _HEADER = struct.Struct("<IfI"): three
// 4-byte fields (12 bytes total), so the PCM payload starts 4-byte aligned.
function encodeFrame(streamId, sampleRate, samples) {
  const header = new ArrayBuffer(12);
  const view = new DataView(header);
  view.setUint32(0, streamId, true);
  view.setFloat32(4, sampleRate, true);
  view.setUint32(8, samples.length, true);
  const payload = new Float32Array(samples).buffer;
  const combined = new Uint8Array(12 + payload.byteLength);
  combined.set(new Uint8Array(header), 0);
  combined.set(new Uint8Array(payload), 12);
  return combined.buffer;
}

class FakeWebSocket {
  static instances = [];

  constructor(url) {
    this.url = url;
    this.binaryType = "";
    this.closed = false;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    FakeWebSocket.instances.push(this);
  }

  close() {
    this.closed = true;
  }
}

const fakeTrack = () => ({ contentHint: "", stop: vi.fn() });
const fakeMediaStreamDestination = () => {
  const tracks = [fakeTrack()];
  return { stream: { getAudioTracks: () => tracks, getTracks: () => tracks } };
};

class FakeAudioWorkletNode {
  static instances = [];

  // Set by a test to a 1-based construction index that should throw,
  // simulating the wet graph failing to build after the dry graph already
  // succeeded.
  static failOnConstruction = null;

  constructor(context, name, options) {
    FakeAudioWorkletNode.instances.push(this);
    if (FakeAudioWorkletNode.instances.length === FakeAudioWorkletNode.failOnConstruction) {
      throw new Error("worklet construction failed");
    }
    this.context = context;
    this.name = name;
    this.options = options;
    this.port = { postMessage: vi.fn() };
    this.connect = vi.fn();
    this.disconnect = vi.fn();
  }
}

class FakeAudioContext {
  static instances = [];

  constructor(options) {
    this.options = options;
    this.state = "running";
    this.audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
    this.destinations = [];
    FakeAudioContext.instances.push(this);
  }

  createMediaStreamDestination() {
    const destination = fakeMediaStreamDestination();
    this.destinations.push(destination);
    return destination;
  }

  async close() {
    this.state = "closed";
  }
}

let createRelayVoiceGraph;

beforeEach(async () => {
  vi.resetModules();
  FakeWebSocket.instances = [];
  FakeAudioWorkletNode.instances = [];
  FakeAudioWorkletNode.failOnConstruction = null;
  FakeAudioContext.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
  vi.stubGlobal("AudioContext", FakeAudioContext);
  vi.stubGlobal("AudioWorkletNode", FakeAudioWorkletNode);
  ({ createRelayVoiceGraph } = await import("../src/services/pythonVoiceRelay.js"));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("python voice relay", () => {
  test("resolves once the first frame arrives and builds distinct dry/wet streams", async () => {
    const promise = createRelayVoiceGraph({ connectTimeoutMs: 500 });
    await Promise.resolve();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    expect(socket).toBeTruthy();
    expect(socket.binaryType).toBe("arraybuffer");

    socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [0.1, 0.2, 0.3]) });
    const graph = await promise;

    expect(graph.stream).toBeTruthy();
    expect(graph.effectsStream).toBeTruthy();
    expect(graph.stream).not.toBe(graph.effectsStream);
    await graph.close();
  });

  test("routes dry and wet frames to their own worklet node", async () => {
    const promise = createRelayVoiceGraph({ connectTimeoutMs: 500 });
    await Promise.resolve();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [1, 2]) });
    await promise;

    const [dryNode, wetNode] = FakeAudioWorkletNode.instances;
    expect(dryNode.port.postMessage).toHaveBeenCalledTimes(1);
    expect(Array.from(dryNode.port.postMessage.mock.calls[0][0])).toEqual([1, 2]);

    socket.onmessage({ data: encodeFrame(STREAM_WET, 48000, [9, 8, 7]) });
    socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [5]) });

    expect(wetNode.port.postMessage).toHaveBeenCalledTimes(1);
    expect(Array.from(wetNode.port.postMessage.mock.calls[0][0])).toEqual([9, 8, 7]);
    expect(dryNode.port.postMessage).toHaveBeenCalledTimes(2);
    expect(Array.from(dryNode.port.postMessage.mock.calls[1][0])).toEqual([5]);
  });

  test("buffers frames that arrive while the AudioWorklet module is still loading", async () => {
    // connectRelaySocket() clears socket.onmessage once it resolves on the
    // first frame; the relay keeps sending frames continuously the whole
    // time, so anything arriving during the async addModule()/node-creation
    // gap that follows used to be silently dropped instead of queued.
    let resolveAddModule;
    class SlowAudioContext extends FakeAudioContext {
      constructor(options) {
        super(options);
        this.audioWorklet = {
          addModule: vi.fn(() => new Promise((resolve) => { resolveAddModule = resolve; }))
        };
      }
    }
    vi.stubGlobal("AudioContext", SlowAudioContext);

    const promise = createRelayVoiceGraph({ connectTimeoutMs: 500 });
    await Promise.resolve();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [1]) });

    // Let the continuation run up to (and pause at) the pending addModule().
    for (let tick = 0; tick < 10 && !resolveAddModule; tick += 1) await Promise.resolve();
    expect(resolveAddModule).toBeTruthy();

    socket.onmessage({ data: encodeFrame(STREAM_WET, 48000, [2, 3]) });
    resolveAddModule();
    const graph = await promise;

    const [dryNode, wetNode] = FakeAudioWorkletNode.instances;
    expect(dryNode.port.postMessage).toHaveBeenCalledTimes(1);
    expect(wetNode.port.postMessage).toHaveBeenCalledTimes(1);
    expect(Array.from(wetNode.port.postMessage.mock.calls[0][0])).toEqual([2, 3]);
    await graph.close();
  });

  test("keeps only the newest startup frames while the worklet is loading", async () => {
    let resolveAddModule;
    class SlowAudioContext extends FakeAudioContext {
      constructor(options) {
        super(options);
        this.audioWorklet = {
          addModule: vi.fn(() => new Promise((resolve) => { resolveAddModule = resolve; }))
        };
      }
    }
    vi.stubGlobal("AudioContext", SlowAudioContext);

    const promise = createRelayVoiceGraph({ connectTimeoutMs: 500 });
    await Promise.resolve();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [0]) });
    for (let tick = 0; tick < 10 && !resolveAddModule; tick += 1) await Promise.resolve();
    expect(resolveAddModule).toBeTruthy();

    for (let index = 1; index <= 40; index += 1) {
      socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [index]) });
    }
    resolveAddModule();
    const graph = await promise;

    const [dryNode] = FakeAudioWorkletNode.instances;
    // One first frame plus no more than ~20ms of fresh startup audio. Old
    // voice must be discarded instead of being replayed late into the room.
    expect(dryNode.port.postMessage.mock.calls.length).toBeLessThanOrEqual(9);
    expect(Array.from(dryNode.port.postMessage.mock.calls.at(-1)[0])).toEqual([40]);
    await graph.close();
  });

  test("stops the dry graph's tracks if the wet graph fails to construct", async () => {
    FakeAudioWorkletNode.failOnConstruction = 2;
    const promise = createRelayVoiceGraph({ connectTimeoutMs: 500 });
    await Promise.resolve();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [1]) });

    await expect(promise).rejects.toThrow("worklet construction failed");

    const [context] = FakeAudioContext.instances;
    expect(context.state).toBe("closed");
    const [dryDestination] = context.destinations;
    expect(dryDestination.stream.getTracks()[0].stop).toHaveBeenCalled();
  });

  test("rejects when the relay closes before sending any frame", async () => {
    const promise = createRelayVoiceGraph({ connectTimeoutMs: 500 });
    await Promise.resolve();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onclose({ code: 4004 });
    await expect(promise).rejects.toThrow(/unavailable/i);
  });

  test("rejects after the connect timeout elapses with no message", async () => {
    vi.useFakeTimers();
    const promise = createRelayVoiceGraph({ connectTimeoutMs: 50 });
    const assertion = expect(promise).rejects.toThrow(/timed out/i);
    await vi.advanceTimersByTimeAsync(60);
    await assertion;
    vi.useRealTimers();
  });

  test("close() closes the socket and stops the reconstructed streams", async () => {
    const promise = createRelayVoiceGraph({ connectTimeoutMs: 500 });
    await Promise.resolve();
    await Promise.resolve();
    const socket = FakeWebSocket.instances[0];
    socket.onmessage({ data: encodeFrame(STREAM_DRY, 48000, [1]) });
    const graph = await promise;

    await graph.close();

    expect(socket.closed).toBe(true);
    expect(graph.stream.getTracks()[0].stop).toHaveBeenCalled();
    expect(graph.effectsStream.getTracks()[0].stop).toHaveBeenCalled();
  });
});
