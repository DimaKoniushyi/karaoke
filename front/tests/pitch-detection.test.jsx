/* @vitest-environment jsdom */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { same, called, calledTimes, verify } from "./helpers/assertions.mjs";
const mocks = vi.hoisted(() => ({ detectMidiFromAnalyser: vi.fn() }));
vi.mock("../src/pages/Karaoke/utils/pitch", () => ({
  detectMidiFromAnalyser: mocks.detectMidiFromAnalyser
}));
vi.mock("../src/services/microphoneCapture", () => ({
  acquireMicrophone: async (preferredDeviceId) => {
    const base = {
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false
    };
    const selected = preferredDeviceId && preferredDeviceId !== "default";
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: selected ? { ...base, deviceId: { exact: preferredDeviceId } } : base
      });
      if (!stream && selected) stream = await navigator.mediaDevices.getUserMedia({ audio: base });
    } catch (error) {
      if (!selected) throw error;
      stream = await navigator.mediaDevices.getUserMedia({ audio: base });
    }
    return {
      stream,
      release: async () => stream?.getTracks?.().forEach((track) => track.stop())
    };
  }
}));
let usePitchDetection;
let frames;
beforeEach(async () => {
  vi.resetModules();
  ({ default: usePitchDetection } = await import("../src/pages/Karaoke/performance-stage/usePitchDetection"));
  frames = [];
  mocks.detectMidiFromAnalyser.mockReset();
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn((callback) => {
      frames.push(callback);
      return frames.length;
    })
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  delete window.AudioContext;
  delete window.webkitAudioContext;
  delete globalThis.electronAPI;
});
const createAudio = ({ state = "running" } = {}) => {
  const analyser = { fftSize: 0, smoothingTimeConstant: 0.2 };
  const source = { connect: vi.fn(), disconnect: vi.fn() };
  const context = {
    state,
    sampleRate: 48000,
    createAnalyser: () => analyser,
    createMediaStreamSource: () => source,
    resume: vi.fn().mockResolvedValue(undefined),
    close: vi.fn().mockResolvedValue(undefined)
  };
  const track = { readyState: "live", stop: vi.fn() };
  const stream = { getAudioTracks: () => [track], getTracks: () => [track] };
  return { analyser, source, context, stream, track };
};
const props = (overrides = {}) => ({
  isPlaying: true,
  monitorInputDeviceId: "default",
  monitoringEnabled: true,
  ...overrides
});
const installAudioContext = (audio) => {
  // Constructor mocks must use a constructable function for Vitest.
  // eslint-disable-next-line prefer-arrow-callback
  window.AudioContext = vi.fn(function AudioContext(options) {
    audio.context.options = options;
    return audio.context;
  });
};
const resetState = {
  sungMidi: null,
  isPitchDetected: false,
  isPitchAttacking: false,
  pitchRestProgress: 1
};
const setMediaDevices = (getUserMedia) => Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
const setupAudio = ({ audio = createAudio(), getUserMedia = vi.fn().mockResolvedValue(audio.stream) } = {}) => {
  setMediaDevices(getUserMedia);
  installAudioContext(audio);
  return { audio, getUserMedia };
};
const runFrame = (timestamp) => act(() => frames.shift()(timestamp));
describe("pitch detection", () => {
  test("releases pitch-capture hardware while minimized and reacquires it after restore", async () => {
    let notify;
    globalThis.electronAPI = {
      isElectron: true,
      onHardwareSuspensionChange: (callback) => {
        notify = callback;
        return () => {};
      }
    };
    const { audio, getUserMedia } = setupAudio();
    renderHook(() => usePitchDetection(props()));
    await waitFor(() => expect(audio.source.connect).toHaveBeenCalled());

    act(() => notify(true));
    await waitFor(() => expect(audio.track.stop).toHaveBeenCalled());
    expect(audio.context.close).toHaveBeenCalled();

    act(() => notify(false));
    await waitFor(() => expect(getUserMedia).toHaveBeenCalledTimes(2));
  });
  test("borrows the room microphone without recapturing or stopping it", async () => {
    const { audio, getUserMedia } = setupAudio();
    const getLocalVoiceStream = vi.fn().mockResolvedValue(audio.stream);
    const input = props({ getLocalVoiceStream });
    const hook = renderHook((value) => usePitchDetection(value), { initialProps: input });
    await waitFor(() => expect(audio.source.connect).toHaveBeenCalled());
    expect(getUserMedia).not.toHaveBeenCalled();
    hook.rerender({ ...input, monitoringEnabled: false });
    expect(getLocalVoiceStream).toHaveBeenCalledTimes(1);
    hook.unmount();
    expect(audio.track.stop).not.toHaveBeenCalled();
    expect(audio.context.close).toHaveBeenCalledTimes(1);
  });
  test("stays reset while playback or media capture is unavailable", () => {
    const hook = renderHook((value) => usePitchDetection(value), {
      initialProps: props({ isPlaying: false })
    });
    expect(hook.result.current).toEqual(resetState);
    hook.rerender(props({ isPlaying: true }));
    expect(frames).toEqual([]);
  });
  test("never initializes audio while playback is paused", async () => {
    const getUserMedia = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    const hook = renderHook(() => usePitchDetection(props({ isPlaying: false })));
    await act(async () => Promise.resolve());
    verify([frames, "toEqual", []], [getUserMedia, "not.toHaveBeenCalled"]);
    hook.unmount();
  });
  test("tracks attack, smoothed pitch and the complete rest transition", async () => {
    const { audio } = setupAudio();
    mocks.detectMidiFromAnalyser.mockReturnValueOnce(69).mockReturnValueOnce(70).mockReturnValue(null);
    const input = props();
    const hook = renderHook(() => usePitchDetection(input));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    const expected = [
      { sungMidi: 69, isPitchDetected: true, isPitchAttacking: true, pitchRestProgress: 0 },
      { sungMidi: 69.42, isPitchDetected: true, isPitchAttacking: true, pitchRestProgress: 0 },
      { sungMidi: 69.42, isPitchDetected: true, isPitchAttacking: true, pitchRestProgress: 0 },
      { sungMidi: 69.42, isPitchDetected: false, isPitchAttacking: false, pitchRestProgress: 0 },
      { sungMidi: null, isPitchDetected: false, isPitchAttacking: false, pitchRestProgress: 1 }
    ];
    for (const [index, timestamp] of [40, 100, 160, 230, 650].entries()) {
      const frame = frames.shift();
      act(() => frame(timestamp));
      expect(hook.result.current).toEqual(expected[index]);
    }
    expect(hook.result.current.sungMidi).toBeNull();
    same(
      [hook.result.current.isPitchDetected, false],
      [hook.result.current.isPitchAttacking, false],
      [hook.result.current.pitchRestProgress, 1]
    );
    expect(audio.source.connect).toHaveBeenCalledWith(audio.analyser);
    hook.unmount();
    expect(audio.source.disconnect).toHaveBeenCalled();
  });
  test("resets a previously detected pitch when playback stops", async () => {
    const { audio } = setupAudio();
    mocks.detectMidiFromAnalyser.mockReturnValue(69);
    const input = props();
    const hook = renderHook((value) => usePitchDetection(value), { initialProps: input });
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    runFrame(40);
    expect(hook.result.current.isPitchDetected).toBe(true);
    hook.rerender({ ...input, isPlaying: false });
    expect(hook.result.current).toEqual(resetState);
  });
  test("resets immediately when an active detector is restarted", async () => {
    const { audio } = setupAudio();
    mocks.detectMidiFromAnalyser.mockReturnValue(69);
    const input = props();
    const hook = renderHook((value) => usePitchDetection(value), { initialProps: input });
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    runFrame(40);
    expect(hook.result.current.isPitchDetected).toBe(true);
    hook.rerender({ ...input, monitorInputDeviceId: "other" });
    expect(hook.result.current).toEqual(resetState);
  });
  test("requests a preferred microphone, falls back and owns its audio graph", async () => {
    const audio = createAudio({ state: "suspended" });
    const getUserMedia = vi.fn().mockRejectedValueOnce(new Error("preferred unavailable")).mockResolvedValueOnce(audio.stream);
    setMediaDevices(getUserMedia);
    installAudioContext(audio);
    const hook = renderHook(() => usePitchDetection(props({ monitorInputDeviceId: "usb-mic" })));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    verify([getUserMedia, "toHaveBeenCalledTimes", 2], [getUserMedia.mock.calls[0][0].audio.deviceId, "toEqual", { exact: "usb-mic" }]);
    verify([
      getUserMedia.mock.calls[0][0].audio,
      "toEqual",
      {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        deviceId: { exact: "usb-mic" }
      }
    ]);
    verify([
      getUserMedia.mock.calls[1][0],
      "toEqual",
      { audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } }
    ]);
    verify([audio.context.options, "toEqual", { latencyHint: "interactive" }], [audio.context.resume, "toHaveBeenCalled"]);
    hook.unmount();
    await waitFor(() => called(audio.track.stop, audio.context.close));
    audio.context.close.mockClear();
  });
  test("does not construct a context when every capture attempt fails", async () => {
    const audio = createAudio();
    setupAudio({ audio, getUserMedia: vi.fn().mockRejectedValue(new Error("denied")) });
    const hook = renderHook(() => usePitchDetection(props()));
    await act(async () => Promise.resolve());
    verify([window.AudioContext, "not.toHaveBeenCalled"], [frames, "toEqual", []]);
    hook.unmount();
  });
  test("falls back from a null preferred capture to the generic microphone", async () => {
    const audio = createAudio();
    const getUserMedia = vi.fn().mockResolvedValueOnce(null).mockResolvedValueOnce(audio.stream);
    setMediaDevices(getUserMedia);
    installAudioContext(audio);
    const hook = renderHook(() => usePitchDetection(props({ monitorInputDeviceId: "preferred" })));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    expect(getUserMedia).toHaveBeenCalledTimes(2);
    hook.unmount();
  });
  test.each(["default", ""])("uses one generic capture candidate for device %j", async (monitorInputDeviceId) => {
    const audio = createAudio();
    const getUserMedia = vi.fn().mockResolvedValue(audio.stream);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia }
    });
    installAudioContext(audio);
    const hook = renderHook(() => usePitchDetection(props({ monitorInputDeviceId })));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    expect(getUserMedia).toHaveBeenCalledOnce();
    verify([getUserMedia, "toHaveBeenCalledWith", { audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false } }]);
    hook.unmount();
  });
  test("resets safely after capture or Web Audio failures", async () => {
    setMediaDevices(vi.fn().mockRejectedValue(new Error("permission denied")));
    const capture = renderHook(() => usePitchDetection(props()));
    await act(async () => Promise.resolve());
    expect(capture.result.current.sungMidi).toBeNull();
    capture.unmount();
    const audio = createAudio({ state: "suspended" });
    audio.context.resume.mockRejectedValue(new Error("resume denied"));
    setupAudio({ audio });
    const resume = renderHook(() => usePitchDetection(props()));
    await act(async () => Promise.resolve());
    expect(resume.result.current.isPitchDetected).toBe(false);
  });
  test("releases resources when capture resolves after unmount", async () => {
    const audio = createAudio();
    audio.context.close.mockRejectedValue(new Error("already closed"));
    let resolveStream;
    setupAudio({
      audio,
      getUserMedia: vi.fn(
        () =>
          new Promise((resolve) => {
            resolveStream = resolve;
          })
      )
    });
    const hook = renderHook(() => usePitchDetection(props()));
    await waitFor(() => expect(resolveStream).toBeTypeOf("function"));
    hook.unmount();
    await act(async () => resolveStream(audio.stream));
    await waitFor(() => called(audio.track.stop, audio.context.close));
  });
  test("handles null and stale capture results after unmount", async () => {
    setMediaDevices(vi.fn().mockResolvedValue(null));
    const empty = renderHook(() => usePitchDetection(props()));
    await act(async () => Promise.resolve());
    expect(empty.result.current.sungMidi).toBeNull();
    empty.unmount();
    let rejectCapture;
    setMediaDevices(
      vi.fn(
        () =>
          new Promise((_resolve, reject) => {
            rejectCapture = reject;
          })
      )
    );
    const stale = renderHook(() => usePitchDetection(props()));
    await waitFor(() => expect(rejectCapture).toBeTypeOf("function"));
    stale.unmount();
    await act(async () => rejectCapture(new Error("late")));
  });
  test("throttles pitch measurement and render frames", async () => {
    const { audio } = setupAudio();
    mocks.detectMidiFromAnalyser.mockReturnValueOnce(null).mockReturnValueOnce(69).mockReturnValue(null);
    const input = props();
    const hook = renderHook(() => usePitchDetection(input));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    for (const timestamp of [40, 45, 80, 85, 200, 210]) {
      const frame = frames.shift();
      act(() => frame(timestamp));
    }
    hook.unmount();
  });
  test("honors exact measurement, animation, attack and rest boundaries", async () => {
    const { audio } = setupAudio();
    mocks.detectMidiFromAnalyser.mockReturnValueOnce(60).mockReturnValueOnce(90).mockReturnValue(null);
    const input = props();
    const hook = renderHook(() => usePitchDetection(input));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    runFrame(34);
    expect(mocks.detectMidiFromAnalyser).not.toHaveBeenCalled();
    runFrame(35);
    expect(mocks.detectMidiFromAnalyser).toHaveBeenCalledOnce();
    runFrame(69);
    expect(mocks.detectMidiFromAnalyser).toHaveBeenCalledOnce();
    runFrame(70);
    verify([mocks.detectMidiFromAnalyser, "toHaveBeenCalledTimes", 2], [hook.result.current.sungMidi, "toBe", 60]);
    runFrame(75);
    const renderedAt75 = hook.result.current.sungMidi;
    runFrame(80);
    expect(hook.result.current.sungMidi).toBe(renderedAt75);
    runFrame(84);
    expect(hook.result.current.sungMidi).toBeCloseTo(60.33, 6);
    runFrame(90);
    expect(hook.result.current.sungMidi).toBeCloseTo(60.33, 6);
    runFrame(164);
    expect(hook.result.current.isPitchAttacking).toBe(true);
    runFrame(165);
    expect(hook.result.current.isPitchAttacking).toBe(false);
    runFrame(180);
    expect(hook.result.current.isPitchDetected).toBe(true);
    runFrame(181);
    same([hook.result.current.isPitchDetected, false], [hook.result.current.pitchRestProgress, 0]);
    runFrame(211);
    expect(hook.result.current.pitchRestProgress).toBe(0);
    runFrame(212);
    expect(hook.result.current.pitchRestProgress).toBeCloseTo(31 / 380, 6);
    hook.unmount();
  });
  test("starts a fresh attack when voice returns during the rest fade", async () => {
    const { audio } = setupAudio();
    mocks.detectMidiFromAnalyser.mockReturnValue(60);
    const input = props();
    const hook = renderHook(() => usePitchDetection(input));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    runFrame(40);
    expect(mocks.detectMidiFromAnalyser).toHaveBeenCalledOnce();
    expect(mocks.detectMidiFromAnalyser.mock.results[0].value).toBe(60);
    verify([hook.result.current, "toEqual", { sungMidi: 60, isPitchDetected: true, isPitchAttacking: true, pitchRestProgress: 0 }]);
    mocks.detectMidiFromAnalyser.mockReturnValue(null);
    runFrame(151);
    same([hook.result.current.isPitchDetected, false], [hook.result.current.isPitchAttacking, false]);
    mocks.detectMidiFromAnalyser.mockReturnValue(65);
    runFrame(190);
    verify([hook.result.current, "toEqual", { sungMidi: 65, isPitchDetected: true, isPitchAttacking: true, pitchRestProgress: 0 }]);
    hook.unmount();
  });
  test("ignores a rejected owned-context close during normal cleanup", async () => {
    const audio = createAudio();
    audio.context.close.mockRejectedValue(new Error("already closed"));
    setupAudio({ audio });
    const hook = renderHook(() => usePitchDetection(props()));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    hook.unmount();
    await act(async () => Promise.resolve());
    expect(audio.context.close).toHaveBeenCalled();
  });
  test("handles missing Web Audio and a cancelled suspended context", async () => {
    const audio = createAudio({ state: "suspended" });
    setMediaDevices(vi.fn().mockResolvedValue(audio.stream));
    const unavailable = renderHook(() => usePitchDetection(props()));
    await act(async () => Promise.resolve());
    expect(unavailable.result.current.sungMidi).toBeNull();
    unavailable.unmount();
    let resolveResume;
    audio.context.resume.mockReturnValue(
      new Promise((resolve) => {
        resolveResume = resolve;
      })
    );
    installAudioContext(audio);
    const cancelled = renderHook(() => usePitchDetection(props()));
    await waitFor(() => expect(resolveResume).toBeTypeOf("function"));
    cancelled.unmount();
    await act(async () => resolveResume());
    expect(frames).toEqual([]);
  });
  test("drops stale frames and keeps only the latest pitch samples", async () => {
    const { audio } = setupAudio();
    mocks.detectMidiFromAnalyser
      .mockReturnValueOnce(64)
      .mockReturnValueOnce(60)
      .mockReturnValueOnce(62)
      .mockReturnValueOnce(61)
      .mockReturnValueOnce(63);
    const input = props();
    const hook = renderHook(() => usePitchDetection(input));
    await waitFor(() => expect(frames.length).toBeGreaterThan(0));
    for (const timestamp of [40, 80, 120, 160, 200]) {
      const frame = frames.shift();
      act(() => frame(timestamp));
    }
    expect(hook.result.current.sungMidi).toBeCloseTo(62.146624, 6);
    const stale = frames.shift();
    const scheduledFrames = requestAnimationFrame.mock.calls.length;
    hook.unmount();
    act(() => stale(200));
    calledTimes([mocks.detectMidiFromAnalyser, 5], [requestAnimationFrame, scheduledFrames]);
  });
});
