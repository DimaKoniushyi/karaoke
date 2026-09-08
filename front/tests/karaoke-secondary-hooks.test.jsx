/* @vitest-environment jsdom */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { same, called, calledWith, verify } from "./helpers/assertions.mjs";
const mocks = vi.hoisted(() => ({
  updateUiPreferences: vi.fn(),
  loadKaraokePreferences: vi.fn(),
  saveKaraokePreferences: vi.fn()
}));
vi.mock("../src/api/client", () => ({ api: { updateUiPreferences: mocks.updateUiPreferences } }));
vi.mock("../src/pages/Karaoke/utils/preferences", async (importOriginal) => ({
  ...(await importOriginal()),
  loadKaraokePreferences: mocks.loadKaraokePreferences,
  saveKaraokePreferences: mocks.saveKaraokePreferences
}));
import useKaraokePreferences from "../src/pages/Karaoke/hooks/useKaraokePreferences.js";
import useMelodyGuide from "../src/pages/Karaoke/hooks/useMelodyGuide.js";
beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.loadKaraokePreferences.mockReturnValue({});
  mocks.saveKaraokePreferences.mockReturnValue(true);
  mocks.updateUiPreferences.mockResolvedValue({});
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete globalThis.AudioContext;
  delete globalThis.webkitAudioContext;
});
describe("karaoke preferences", () => {
  test("loads defaults and persists every preference change", async () => {
    const { result } = renderHook(() => useKaraokePreferences());
    verify([
      result.current,
      "toMatchObject",
      {
        musicVolume: 1,
        vocalVolume: 1,
        melodyVolume: 0,
        speed: 1,
        keyShift: 0,
        showLyrics: true,
        showNotes: true,
        autoHideConsole: true,
        effectPreset: "studio",
        timingOffsets: {}
      }
    ]);
    act(() => {
      result.current.setMusicVolume(0.5);
      result.current.setVocalVolume(0.4);
      result.current.setMelodyVolume(0.3);
      result.current.setSpeed(1.2);
      result.current.setKeyShift(2);
      result.current.setShowLyrics(false);
      result.current.setShowNotes(false);
      result.current.setAutoHideConsole(false);
      result.current.setEffectPreset("hall");
      result.current.setTimingOffsets({ song: -3.1 });
    });
    await act(async () => Promise.resolve());
    verify([
      mocks.saveKaraokePreferences,
      "toHaveBeenLastCalledWith",
      {
        musicVolume: 0.5,
        vocalVolume: 0.4,
        melodyVolume: 0.3,
        speed: 1.2,
        keyShift: 2,
        showLyrics: false,
        showNotes: false,
        autoHideConsole: false,
        effectPreset: "hall",
        timingOffsets: { song: -3.1 }
      }
    ]);
    verify([
      mocks.updateUiPreferences,
      "toHaveBeenLastCalledWith",
      "karaoke",
      {
        musicVolume: 0.5,
        vocalVolume: 0.4,
        melodyVolume: 0.3,
        speed: 1.2,
        keyShift: 2,
        showLyrics: false,
        showNotes: false,
        autoHideConsole: false,
        effectPreset: "hall",
        timingOffsets: { song: -3.1 }
      }
    ]);
  });
  test("uses saved values and skips remote persistence when local save fails", () => {
    mocks.loadKaraokePreferences.mockReturnValue({
      musicVolume: 0,
      vocalVolume: 0,
      melodyVolume: 1,
      speed: 0.8,
      keyShift: -2,
      showLyrics: false,
      showNotes: false,
      autoHideConsole: false,
      effectPreset: "dry"
    });
    mocks.saveKaraokePreferences.mockReturnValue(false);
    const { result } = renderHook(() => useKaraokePreferences());
    same([result.current.musicVolume, 0], [result.current.effectPreset, "dry"]);
    expect(mocks.updateUiPreferences).not.toHaveBeenCalled();
  });
  test("ignores optional remote preference persistence failures", async () => {
    mocks.updateUiPreferences.mockRejectedValue(new Error("offline"));
    const { result } = renderHook(() => useKaraokePreferences());
    act(() => result.current.setSpeed(1.1));
    await act(async () => Promise.resolve());
    expect(mocks.updateUiPreferences).toHaveBeenCalled();
  });
});
const guideProps = (overrides = {}) => ({
  notes: [{ start: 0, end: 1, note: 60 }],
  volume: 1,
  keyShift: 0,
  currentTimeRef: { current: 0 },
  ...overrides
});
const renderGuide = (overrides) => renderHook(() => useMelodyGuide(guideProps(overrides)));
function installGuideContext({ resumeError, closeError } = {}) {
  const oscillator = {
    type: "",
    frequency: { setTargetAtTime: vi.fn() },
    connect: vi.fn(),
    start: vi.fn(),
    stop: vi.fn()
  };
  const gain = {
    gain: {
      value: 0,
      setTargetAtTime: vi.fn(),
      cancelScheduledValues: vi.fn(),
      setValueAtTime: vi.fn()
    },
    connect: vi.fn()
  };
  oscillator.connect.mockReturnValue(gain);
  gain.connect.mockReturnValue(gain);
  const context = {
    state: "suspended",
    currentTime: 3,
    destination: {},
    createOscillator: () => oscillator,
    createGain: () => gain,
    resume: vi.fn(() => (resumeError ? Promise.reject(resumeError) : Promise.resolve())),
    close: vi.fn(() => (closeError ? Promise.reject(closeError) : Promise.resolve()))
  };
  globalThis.AudioContext = class {
    constructor(options) {
      context.options = options;
      return context;
    }
  };
  return { context, oscillator, gain };
}
describe("melody guide", () => {
  test("starts, updates, silences and disposes its oscillator", async () => {
    const audio = installGuideContext();
    const currentTimeRef = { current: 1 };
    const hook = renderHook((props) => useMelodyGuide(props), {
      initialProps: {
        notes: [{ start: 0, end: 2, note: 69 }],
        volume: 0.5,
        keyShift: 0,
        currentTimeRef
      }
    });
    await expect(hook.result.current.startMelodyGuide()).resolves.toBe(true);
    await expect(hook.result.current.startMelodyGuide()).resolves.toBe(true);
    expect(audio.oscillator.start).toHaveBeenCalledOnce();
    verify([audio.context.options, "toEqual", { latencyHint: "interactive" }], [audio.oscillator.type, "toBe", "triangle"]);
    verify([audio.oscillator.frequency.setTargetAtTime, "toHaveBeenCalledWith", 440, 3, 0.012]);
    verify([audio.gain.gain.setTargetAtTime, "toHaveBeenCalledWith", 0.3 * 0.5 ** 1.65, 3, 0.02]);
    calledWith([audio.gain.gain.cancelScheduledValues, [3]], [audio.gain.gain.setValueAtTime, [0.0001, 3]]);
    const frequencyCalls = audio.oscillator.frequency.setTargetAtTime.mock.calls.length;
    act(() => hook.result.current.updateMelodyGuide(5));
    verify([audio.oscillator.frequency.setTargetAtTime, "toHaveBeenCalledTimes", frequencyCalls]);
    verify([audio.gain.gain.setTargetAtTime, "toHaveBeenLastCalledWith", 0.0001, 3, 0.018]);
    act(() => hook.result.current.silenceMelodyGuide());
    calledWith([audio.gain.gain.cancelScheduledValues, [3]], [audio.gain.gain.setValueAtTime, [0.0001, 3]]);
    hook.unmount();
    called(audio.oscillator.stop, audio.context.close);
  });
  test("articulates every new lyricsSync note", async () => {
    const audio = installGuideContext();
    const hook = renderHook((props) => useMelodyGuide(props), {
      initialProps: {
        notes: [
          { start: 0, end: 1, note: 69 },
          { start: 1, end: 2, note: 71 }
        ],
        volume: 0.5,
        keyShift: 0,
        currentTimeRef: { current: 0.5 }
      }
    });

    await hook.result.current.startMelodyGuide();
    act(() => hook.result.current.updateMelodyGuide(1.5));

    expect(audio.gain.gain.setValueAtTime).toHaveBeenCalledTimes(2);
    expect(audio.oscillator.frequency.setTargetAtTime).toHaveBeenLastCalledWith(expect.closeTo(493.883, 2), 3, 0.012);
    hook.unmount();
  });
  test("rejects missing inputs and cleans a guide whose resume fails", async () => {
    installGuideContext();
    const empty = renderHook(() => useMelodyGuide(guideProps({ notes: [] })));
    act(() => empty.result.current.updateMelodyGuide(0));
    act(() => empty.result.current.silenceMelodyGuide());
    expect(await empty.result.current.startMelodyGuide()).toBe(false);
    empty.unmount();
    for (const props of [
      { notes: null, volume: 1 },
      { notes: [{ start: 0, end: 1, note: 60 }], volume: 0 }
    ]) {
      const invalid = renderHook(() => useMelodyGuide(guideProps(props)));
      expect(await invalid.result.current.startMelodyGuide()).toBe(false);
      invalid.unmount();
    }
    delete globalThis.AudioContext;
    const unavailable = renderGuide();
    expect(await unavailable.result.current.startMelodyGuide()).toBe(false);
    unavailable.unmount();
    const failure = new Error("resume failed");
    const audio = installGuideContext({
      resumeError: failure,
      closeError: new Error("already closed")
    });
    const failed = renderGuide();
    await expect(failed.result.current.startMelodyGuide()).rejects.toThrow("resume failed");
    called(audio.oscillator.stop, audio.context.close);
  });
  test("closed guides ignore updates and are recreated on start", async () => {
    const first = installGuideContext();
    const hook = renderGuide();
    await hook.result.current.startMelodyGuide();
    first.context.state = "closed";
    const gainCalls = first.gain.gain.setTargetAtTime.mock.calls.length;
    const cancelCalls = first.gain.gain.cancelScheduledValues.mock.calls.length;
    act(() => hook.result.current.updateMelodyGuide(0));
    act(() => hook.result.current.silenceMelodyGuide());
    verify(
      [first.gain.gain.setTargetAtTime, "toHaveBeenCalledTimes", gainCalls],
      [first.gain.gain.cancelScheduledValues, "toHaveBeenCalledTimes", cancelCalls]
    );
    const second = installGuideContext();
    await expect(hook.result.current.startMelodyGuide()).resolves.toBe(true);
    expect(second.oscillator.start).toHaveBeenCalledOnce();
    hook.unmount();
  });
  test("start follows a replaced playback clock ref", async () => {
    const audio = installGuideContext();
    const hook = renderHook((props) => useMelodyGuide(props), {
      initialProps: {
        notes: [{ start: 0, end: 1, note: 60 }],
        volume: 1,
        keyShift: 0,
        currentTimeRef: { current: 5 }
      }
    });
    hook.rerender(guideProps({ currentTimeRef: { current: 0.5 } }));
    await hook.result.current.startMelodyGuide();
    expect(audio.oscillator.frequency.setTargetAtTime).toHaveBeenCalled();
    hook.unmount();
  });
  test("ignores an asynchronous close failure during disposal", async () => {
    const audio = installGuideContext({ closeError: new Error("already closed") });
    const hook = renderGuide();
    await hook.result.current.startMelodyGuide();
    hook.unmount();
    await act(async () => Promise.resolve());
    expect(audio.context.close).toHaveBeenCalled();
  });
  test("does not restore a guide disposed during a failed resume", async () => {
    let rejectResume;
    const audio = installGuideContext();
    audio.context.resume.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectResume = reject;
      })
    );
    const hook = renderGuide();
    const start = hook.result.current.startMelodyGuide();
    hook.unmount();
    rejectResume(new Error("disposed"));
    await expect(start).rejects.toThrow("disposed");
  });
});
