import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

let Processor;

beforeEach(async () => {
  vi.resetModules();
  class FakeAudioWorkletProcessor {
    constructor() {
      this.port = { onmessage: null, postMessage: vi.fn() };
    }
  }
  vi.stubGlobal("AudioWorkletProcessor", FakeAudioWorkletProcessor);
  vi.stubGlobal("sampleRate", 48_000);
  vi.stubGlobal("currentTime", 0);
  vi.stubGlobal("registerProcessor", (_name, implementation) => {
    Processor = implementation;
  });
  await import("../src/services/relayPlaybackProcessor.js");
});

afterEach(() => vi.unstubAllGlobals());

describe("relay playback low-latency queue", () => {
  test("drops stale voice instead of retaining more than 20ms", () => {
    const processor = new Processor({ processorOptions: { sourceRate: 48_000 } });
    processor.port.onmessage({ data: new Float32Array(48_000 * 0.2) });
    expect(processor.used).toBeLessThanOrEqual(48_000 * 0.02);
    expect(processor.dropped).toBeGreaterThan(0);
  });
});
