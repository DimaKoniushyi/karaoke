import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createGraph: vi.fn()
}));

vi.mock("../src/services/microphoneStudioQuality.js", () => ({
  createStudioMicrophoneGraph: mocks.createGraph
}));

describe("central microphone capture", () => {
  let getUserMedia;
  let graph;

  beforeEach(() => {
    vi.resetModules();
    const processedStream = {
      getAudioTracks: () => [{ readyState: "live" }]
    };
    // Liveness is checked against the raw physical capture (see
    // microphoneCapture.js's resolve()), not the graph's processed output --
    // a MediaStreamDestination stays "live" even after the real microphone
    // is unplugged, so it can't be used to detect that.
    const rawStream = { id: "raw", getAudioTracks: () => [{ readyState: "live" }] };
    graph = {
      stream: processedStream,
      rawStream,
      getStream: vi.fn(({ disabledEffects = false } = {}) => (disabledEffects ? rawStream : processedStream)),
      close: vi.fn().mockResolvedValue(undefined),
      replaceInput: vi.fn().mockResolvedValue(undefined),
      setNoiseSuppression: vi.fn()
    };
    mocks.createGraph.mockReset().mockReturnValue(graph);
    getUserMedia = vi.fn().mockResolvedValueOnce({ id: "default" }).mockResolvedValueOnce({ id: "studio" });
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia } }
    });
  });

  afterEach(() => vi.restoreAllMocks());

  test("shares one processed stream and closes it after the final lease", async () => {
    const { acquireMicrophone } = await import("../src/services/microphoneCapture.js");
    const first = await acquireMicrophone();
    const second = await acquireMicrophone();

    expect(first.stream).toBe(second.stream);
    expect(getUserMedia).toHaveBeenCalledOnce();
    await first.release();
    expect(graph.close).not.toHaveBeenCalled();
    await second.release();
    expect(graph.close).toHaveBeenCalledOnce();
  });

  test("can bypass effects for one lease without changing other microphone users", async () => {
    const { acquireMicrophone } = await import("../src/services/microphoneCapture.js");
    const clean = await acquireMicrophone("", { disabledEffects: true });
    const processed = await acquireMicrophone();

    expect(clean.stream).toBe(graph.rawStream);
    expect(processed.stream).toBe(graph.stream);
    expect(getUserMedia).toHaveBeenCalledOnce();
    expect(graph.getStream).toHaveBeenNthCalledWith(1, { disabledEffects: true });
    expect(graph.getStream).toHaveBeenNthCalledWith(2, { disabledEffects: false });
    await clean.release();
    await processed.release();
  });

  test("switches the physical input without replacing the shared output stream", async () => {
    const { acquireMicrophone } = await import("../src/services/microphoneCapture.js");
    const defaultLease = await acquireMicrophone();
    const selectedLease = await acquireMicrophone("studio-mic");

    expect(getUserMedia).toHaveBeenLastCalledWith({
      audio: expect.objectContaining({ deviceId: { exact: "studio-mic" } })
    });
    expect(graph.replaceInput).toHaveBeenCalledWith({ id: "studio" });
    expect(selectedLease.stream).toBe(defaultLease.stream);
    await defaultLease.release();
    await selectedLease.release();
  });

  test("release() does not run until a device switch already queued ahead of it finishes", async () => {
    // release() used to mutate `active`/`entry.users` immediately, outside
    // the acquire queue. A release landing while a concurrent device switch
    // was still mid-await (capturing the new device, then replaceInput())
    // could close the very graph the switch was still using, or null out
    // `active` out from under it.
    const { acquireMicrophone } = await import("../src/services/microphoneCapture.js");
    const first = await acquireMicrophone();

    let resolveSwitchCapture;
    getUserMedia = vi.fn(
      () => new Promise((resolve) => { resolveSwitchCapture = () => resolve({ id: "studio" }); })
    );
    navigator.mediaDevices.getUserMedia = getUserMedia;
    let resolveReplaceInput;
    graph.replaceInput.mockImplementationOnce(
      () => new Promise((resolve) => { resolveReplaceInput = resolve; })
    );

    const switching = acquireMicrophone("studio-mic");
    const releasing = first.release();

    await Promise.resolve();
    await Promise.resolve();
    expect(graph.close).not.toHaveBeenCalled();

    resolveSwitchCapture();
    for (let tick = 0; tick < 10 && !resolveReplaceInput; tick += 1) await Promise.resolve();
    resolveReplaceInput();
    const second = await switching;
    await releasing;
    // The switch (queued first) has now finished and still holds a lease,
    // so release()'s own cleanup -- which ran after it -- must not have
    // closed the shared graph yet.
    expect(graph.close).not.toHaveBeenCalled();
    await second.release();
    expect(graph.close).toHaveBeenCalledOnce();
  });

  test("closes an orphaned entry once its own last lease releases, even after a replacement became active", async () => {
    // resolve() replaces `active` outright (a brand new entry/graph) when the
    // previous one's physical device died, instead of reusing it. A caller
    // still holding a lease on that superseded entry must still get its
    // graph closed when it eventually releases -- skipping that because the
    // entry is no longer `active` used to leak its whole Web Audio graph
    // (AudioContext included) forever.
    const { acquireMicrophone } = await import("../src/services/microphoneCapture.js");
    const firstGraph = graph;
    const firstLease = await acquireMicrophone();

    firstGraph.rawStream.getAudioTracks = () => [{ readyState: "ended" }];
    const secondGraph = {
      ...firstGraph,
      close: vi.fn().mockResolvedValue(undefined),
      rawStream: { id: "raw2", getAudioTracks: () => [{ readyState: "live" }] }
    };
    mocks.createGraph.mockReturnValueOnce(secondGraph);
    getUserMedia = vi.fn().mockResolvedValueOnce({ id: "second" });
    navigator.mediaDevices.getUserMedia = getUserMedia;

    const secondLease = await acquireMicrophone();
    expect(mocks.createGraph).toHaveBeenCalledTimes(2);

    await firstLease.release();
    expect(firstGraph.close).toHaveBeenCalledOnce();
    expect(secondGraph.close).not.toHaveBeenCalled();

    await secondLease.release();
    expect(secondGraph.close).toHaveBeenCalledOnce();
  });

  test("falls back to the default input when the selected device disappeared", async () => {
    getUserMedia = vi.fn().mockRejectedValueOnce(new Error("missing device")).mockResolvedValueOnce({ id: "fallback" });
    navigator.mediaDevices.getUserMedia = getUserMedia;
    const { acquireMicrophone } = await import("../src/services/microphoneCapture.js");
    const lease = await acquireMicrophone("missing");

    expect(getUserMedia).toHaveBeenCalledTimes(2);
    expect(mocks.createGraph).toHaveBeenCalledWith({ id: "fallback" });
    await lease.release();
  });
});
