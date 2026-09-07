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

  constructor(context, name, options) {
    this.context = context;
    this.name = name;
    this.options = options;
    this.port = { postMessage: vi.fn() };
    this.connect = vi.fn();
    this.disconnect = vi.fn();
    FakeAudioWorkletNode.instances.push(this);
  }
}

class FakeAudioContext {
  constructor(options) {
    this.options = options;
    this.state = "running";
    this.audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) };
  }

  createMediaStreamDestination() {
    return fakeMediaStreamDestination();
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
