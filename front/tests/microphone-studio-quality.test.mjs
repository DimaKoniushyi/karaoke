import { afterEach, describe, expect, test, vi } from "vitest";
import { readFileSync } from "node:fs";
import { createStudioMicrophoneGraph } from "../src/services/microphoneStudioQuality.js";
import { verify } from "./helpers/assertions.mjs";
class Param {
  constructor() {
    this.value = 0;
  }
}
class Node {
  constructor() {
    this.connections = [];
    this.disconnections = [];
    ["frequency", "Q", "gain", "threshold", "knee", "ratio", "attack", "release"].forEach((name) => {
      this[name] = new Param();
    });
  }
  connect(target) {
    this.target = target;
    this.connections.push(target);
    return target;
  }
  disconnect(target) {
    this.disconnections.push(target);
  }
}
describe("studio microphone quality", () => {
  afterEach(() => {
    delete globalThis.AudioContext;
  });
  test("builds an always-on cleanup graph through the shared channel strip", async () => {
    // Room self-monitor and room outgoing-to-peers must sound identical --
    // both now build the exact same node chain (see
    // src/services/microphoneChannelStrip.js) instead of two independently
    // hand-tuned graphs.
    const processedTrack = { kind: "audio", contentHint: "", stop: vi.fn() };
    const effectsTrack = { kind: "audio", contentHint: "", stop: vi.fn() };
    const destinations = [processedTrack, effectsTrack].map((mediaTrack) => ({
      stream: { getAudioTracks: () => [mediaTrack], getTracks: () => [mediaTrack] }
    }));
    const created = { filters: [], compressors: [], gains: [], shapers: [] };
    const output = new Node();
    let contextOptions;
    globalThis.AudioContext = class {
      constructor(options) {
        contextOptions = options;
        this.state = "running";
        this.destination = output;
      }
      createMediaStreamSource() {
        return new Node();
      }
      createBiquadFilter() {
        const node = new Node();
        created.filters.push(node);
        return node;
      }
      createDynamicsCompressor() {
        const node = new Node();
        created.compressors.push(node);
        return node;
      }
      createGain() {
        const node = new Node();
        created.gains.push(node);
        return node;
      }
      createWaveShaper() {
        const node = new Node();
        created.shapers.push(node);
        return node;
      }
      createMediaStreamDestination() {
        return destinations.shift();
      }
      resume() {
        return Promise.resolve();
      }
      close() {
        this.state = "closed";
        return Promise.resolve();
      }
    };
    const rawTrack = { kind: "audio", stop: vi.fn() };
    const rawStream = { getTracks: () => [rawTrack] };
    const graph = createStudioMicrophoneGraph(rawStream);
    expect(contextOptions).toEqual({ latencyHint: 0 });
    verify(
      [graph.stream.getAudioTracks()[0], "toBe", processedTrack],
      [graph.getStream(), "toBe", graph.stream],
      [graph.effectsStream.getAudioTracks()[0], "toBe", effectsTrack],
      [graph.getStream({ effectsEnabled: true }), "toBe", graph.effectsStream],
      [graph.getStream({ disabledEffects: true }), "toBe", rawStream],
      [created.filters.map((node) => node.type), "toEqual", ["highpass", "highshelf"]],
      [created.compressors, "toHaveLength", 1],
      [created.shapers, "toHaveLength", 1],
      [processedTrack.contentHint, "toBe", "music"]
    );
    expect(graph.setMonitoring(true, { volume: 0.7 })).toBe(true);
    const monitorGain = created.gains.at(-1);
    expect(monitorGain.gain.value).toBe(1);
    expect(monitorGain.connections).toContain(output);
    const finalOutput = created.gains.find((node) => node.connections.includes(monitorGain));
    expect(finalOutput).toBeDefined();
    expect(graph.setMonitoring(false)).toBe(false);
    expect(finalOutput.disconnections).toContain(monitorGain);
    await graph.close();
    expect(rawTrack.stop).toHaveBeenCalledOnce();
    expect(processedTrack.stop).toHaveBeenCalledOnce();
    expect(effectsTrack.stop).toHaveBeenCalledOnce();
  });
  test("degrades to the raw stream without leaking capture when WebAudio is unavailable", async () => {
    const track = { stop: vi.fn() };
    const rawStream = { getTracks: () => [track] };
    const graph = createStudioMicrophoneGraph(rawStream);
    expect(graph.stream).toBe(rawStream);
    expect(graph.setMonitoring(true)).toBe(false);
    await graph.close();
    expect(track.stop).toHaveBeenCalledOnce();
  });
  test("room fallback pitch shift keeps the same low-latency window as native monitoring", () => {
    const source = readFileSync(new URL("../src/services/pitchShiftProcessor.js", import.meta.url), "utf8");
    expect(source).toContain("sampleRate * 0.012");
    expect(source).not.toContain("this.bufferLength = 1536");
  });
  test("rolls back the raw stream when graph construction fails", () => {
    const track = { stop: vi.fn() };
    const rawStream = { getTracks: () => [track] };
    globalThis.AudioContext = class {
      constructor() {
        this.state = "running";
      }
      createMediaStreamSource() {
        throw new Error("audio graph failed");
      }
      close() {
        this.state = "closed";
        return Promise.resolve();
      }
    };
    expect(() => createStudioMicrophoneGraph(rawStream)).toThrow("audio graph failed");
    expect(track.stop).toHaveBeenCalledOnce();
  });
});
