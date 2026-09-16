/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { installFrameQueue, suppressWindowErrors } from "./helpers/browser.mjs";
import { same, notCalled, calledTimes, verify } from "./helpers/assertions.mjs";
const mocks = vi.hoisted(() => ({ updateUiPreferences: vi.fn(), setDirectMonitorMediaActive: vi.fn() }));
vi.mock("../src/api/client", () => ({ api: mocks }));
let RADIO_STATIONS;
let RadioProvider;
let calculateRadioLightingPulse;
let calculateRadioSpectrum;
let isAutoplayBlocked;
let normalizeRadioSettings;
let useRadio;
const wrapper = ({ children }) => <RadioProvider>{children}</RadioProvider>;
const store = (value) => localStorage.setItem("karaoke-radio", JSON.stringify(value));
const installAudio = ({
  state = "running",
  getByteFrequencyData = vi.fn(),
  resume = vi.fn(),
  close = vi.fn().mockResolvedValue(),
  onConstruct
} = {}) => {
  const analyser = {
    fftSize: 0,
    smoothingTimeConstant: 0,
    frequencyBinCount: 32,
    connect: vi.fn(),
    getByteFrequencyData
  };
  const context = {
    state,
    sampleRate: 48000,
    destination: {},
    createAnalyser: vi.fn(() => analyser),
    createMediaElementSource: vi.fn(() => ({ connect: vi.fn() })),
    resume,
    close
  };
  window.AudioContext = class {
    constructor() {
      onConstruct?.();
      return context;
    }
  };
  return { analyser, context };
};
beforeEach(async () => {
  vi.resetModules();
  ({ RADIO_STATIONS, RadioProvider, calculateRadioLightingPulse, calculateRadioSpectrum, isAutoplayBlocked, normalizeRadioSettings, useRadio } =
    await import("../src/contexts/radio.jsx"));
  localStorage.clear();
  store({ stationId: "poptron", volume: 0.45, enabled: false });
  mocks.updateUiPreferences.mockReset().mockResolvedValue({});
  mocks.setDirectMonitorMediaActive.mockReset().mockResolvedValue({});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(function pause() {
    Object.defineProperty(this, "paused", { configurable: true, value: true });
  });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockImplementation(function play() {
    Object.defineProperty(this, "paused", { configurable: true, value: false });
    return Promise.resolve();
  });
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn(() => 1)
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("--radio-bass");
  document.documentElement.style.removeProperty("--radio-analysis-active");
  for (let index = 0; index < 18; index += 1) document.documentElement.style.removeProperty(`--radio-band-${index}`);
  delete window.AudioContext;
  delete window.webkitAudioContext;
});
describe("radio context", () => {
  test("keeps the complete station catalog contract", () => {
    expect(RADIO_STATIONS).toHaveLength(40);
    const groups = Object.groupBy(RADIO_STATIONS, ({ group }) => group);
    expect(Object.keys(groups)).toHaveLength(8);
    Object.values(groups).forEach((stations) => expect(stations).toHaveLength(5));
    expect(RADIO_STATIONS[0]).toEqual(
      expect.objectContaining({
        id: "poptron",
        name: "SomaFM PopTron",
        streams: ["https://ice5.somafm.com/poptron-128-mp3", "https://ice2.somafm.com/poptron-128-mp3"]
      })
    );
    RADIO_STATIONS.forEach(({ description, group, streams }) => {
      verify([description, "not.toBe", ""], [group, "not.toBe", ""], [streams, "toHaveLength", 2]);
    });
  });
  test("normalizes settings and classifies autoplay failures", () => {
    expect(normalizeRadioSettings()).toEqual({ enabled: true, stationId: "poptron", volume: 0.1 });
    verify([
      normalizeRadioSettings({ stationId: "indiepop", volume: -1, enabled: false }),
      "toEqual",
      { enabled: false, stationId: "indiepop", volume: 0 }
    ]);
    same(
      [isAutoplayBlocked({ name: "NotAllowedError" }), true],
      [isAutoplayBlocked({ message: "user didn't interact" }), true],
      [isAutoplayBlocked("not allowed by browser"), true],
      [isAutoplayBlocked(null), false],
      [isAutoplayBlocked(new Error("offline")), false]
    );
  });
  test("calculates deterministic attack, decay and frequency bands", () => {
    const data = Uint8Array.from({ length: 64 }, (_, index) => (index * 7) % 256);
    const result = calculateRadioSpectrum(
      data,
      48000,
      2048,
      0.2,
      Array.from({ length: 18 }, (_, index) => index / 40)
    );
    expect(result.bass).toBeCloseTo(0.19082352941176473, 14);
    verify([
      result.bands,
      "toEqual",
      [
        0.045217254901960775, 0.07832588235294116, 0.11143450980392156, 0.15584745098039215, 0.2228690196078431, 0.35373607843137256,
        0.46008392156862743, 0.6075094117647059, 0.6639999999999999, 0.5684728506787331, 0.486435294117647, 0.6955, 0.252, 0.273, 0.294,
        0.315, 0.336, 0.357
      ]
    ]);
    const decay = calculateRadioSpectrum(new Uint8Array(64), 48000, 2048, 0.5, Array(18).fill(0.5));
    expect(decay).toEqual({ bands: Array(18).fill(0.42), bass: 0.44 });
    const attack = calculateRadioSpectrum(new Uint8Array(1025).fill(50), 48000, 2048, 0, Array(18).fill(0));
    expect(attack.bass).toBeCloseTo(0.09019607843137255, 14);
    verify([attack.bands.slice(0, 5), "toEqual", Array(5).fill(0.16149019607843135)]);
    verify([attack.bands.slice(5, 12), "toEqual", Array(7).fill(0.19560784313725488)]);
    expect(attack.bands.slice(12)).toEqual(Array(6).fill(0.23313725490196074));
    const lowSampleRate = calculateRadioSpectrum(data, 8000, 2048, 0.2, Array(18).fill(0));
    expect(lowSampleRate.bass).toBeCloseTo(0.32825882352941177, 14);
    verify([
      lowSampleRate.bands.slice(0, 8),
      "toEqual",
      [0.293912156862745, 0.37304235294117644, 0.4747811764705881, 0.58, 0.5319487058823529, 0.20343215686274507, 0.5320533333333333, 0.58]
    ]);
  });
  test("detects kick, clap and hi-hat attacks instead of saturated radio loudness", () => {
    const bed = Array(18).fill(0.82);
    const hit = (from, to, amount) =>
      bed.map((value, index) => (index >= from && index < to ? value + amount : value));
    const kick = calculateRadioLightingPulse(hit(0, 6, 0.1), bed, 0.08);
    const clap = calculateRadioLightingPulse(hit(6, 13, 0.08), bed, 0.08);
    const tick = calculateRadioLightingPulse(hit(13, 18, 0.06), bed, 0.08);
    const steady = calculateRadioLightingPulse(bed, bed, 0.8);

    expect(kick).toBeGreaterThan(0.45);
    expect(clap).toBeGreaterThan(0.45);
    expect(tick).toBeGreaterThan(0.4);
    expect(steady).toBeLessThan(0.8);
    expect(steady).toBeGreaterThan(0);
  });
  test("forgets an old lighting hit before the next beat instead of building a bright bed", () => {
    const bed = Array(18).fill(0.12);
    const hit = bed.map((value, index) => (index < 6 ? value + 0.12 : value));
    let pulse = calculateRadioLightingPulse(hit, bed, 0.04);
    const peak = pulse;

    for (let frame = 0; frame < 10; frame += 1) {
      pulse = calculateRadioLightingPulse(bed, bed, pulse);
    }

    expect(peak).toBeGreaterThan(0.4);
    expect(pulse).toBeLessThan(0.06);
    expect(calculateRadioLightingPulse(hit, bed, pulse)).toBeGreaterThan(0.4);
  });
  test("requires the provider", () => {
    const { log, restore } = suppressWindowErrors();
    verify([() => renderHook(() => useRadio()), "toThrow", "useRadio must be used inside RadioProvider"]);
    restore();
  });
  test("normalizes stored settings and exposes station metadata", () => {
    store({ stationId: "unknown", volume: 8, enabled: "yes" });
    const { result } = renderHook(() => useRadio(), { wrapper });
    same(
      [result.current.stationId, "poptron"],
      [result.current.volume, 1],
      [result.current.stations, RADIO_STATIONS],
      [result.current.getBassLevel(), 0]
    );
    expect(result.current.getSpectrumLevels()).toHaveLength(18);
    cleanup();
    store({ stationId: "poptron", volume: "bad", enabled: false });
    const fallback = renderHook(() => useRadio(), { wrapper });
    same(
      [fallback.result.current.volume, 0.1],
      [fallback.result.current.isPlaying, false],
      [fallback.result.current.isLoading, false],
      [fallback.result.current.error, ""]
    );
  });
  test("starts, stops, toggles and persists playback", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      expect(await hook.result.current.turnOn()).toBe(true);
    });
    verify([hook.result.current.isPlaying, "toBe", true], [document.querySelector("audio").src, "toContain", "poptron"]);
    verify([mocks.updateUiPreferences, "toHaveBeenCalledWith", "radio", expect.objectContaining({ enabled: true })]);
    act(() => hook.result.current.toggle());
    expect(hook.result.current.isPlaying).toBe(false);
    verify([mocks.updateUiPreferences, "toHaveBeenLastCalledWith", "radio", expect.objectContaining({ enabled: false })]);
    act(() => hook.result.current.toggle());
    await act(async () => Promise.resolve());
    expect(hook.result.current.isPlaying).toBe(true);
  });
  test("releases exclusive monitoring before radio playback", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => hook.result.current.turnOn());
    expect(mocks.setDirectMonitorMediaActive).toHaveBeenCalledWith(true);
    expect(mocks.setDirectMonitorMediaActive.mock.invocationCallOrder[0]).toBeLessThan(
      HTMLMediaElement.prototype.play.mock.invocationCallOrder[0]
    );
    act(() => hook.result.current.turnOff());
    expect(mocks.setDirectMonitorMediaActive).toHaveBeenLastCalledWith(false);
  });
  test("radio still plays when the monitor backend is unavailable", async () => {
    mocks.setDirectMonitorMediaActive.mockRejectedValueOnce(new Error("backend offline"));
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => expect(await hook.result.current.turnOn()).toBe(true));
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalled();
  });
  test("exposes pending playback state and applies startup options", async () => {
    let resolvePlay;
    HTMLMediaElement.prototype.play.mockImplementationOnce(function playPending() {
      Object.defineProperty(this, "paused", { configurable: true, value: false });
      return new Promise((resolve) => {
        resolvePlay = resolve;
      });
    });
    const { analyser } = installAudio();
    const hook = renderHook(() => useRadio(), { wrapper });
    fireEvent.error(document.querySelector("audio"));
    expect(hook.result.current.error).not.toBe("");
    let pending;
    act(() => {
      pending = hook.result.current.turnOn({ remember: false });
    });
    await act(async () => Promise.resolve());
    same([hook.result.current.error, ""], [hook.result.current.isLoading, true], [document.querySelector("audio").volume, 0.45]);
    resolvePlay();
    await act(async () => expect(await pending).toBe(true));
    verify([analyser.getByteFrequencyData, "toHaveBeenCalledTimes", 1], [mocks.updateUiPreferences, "not.toHaveBeenCalled"]);
    hook.result.current.turnOff({ remember: false });
    let resolveFade;
    HTMLMediaElement.prototype.play.mockImplementationOnce(function playPendingFade() {
      Object.defineProperty(this, "paused", { configurable: true, value: false });
      return new Promise((resolve) => {
        resolveFade = resolve;
      });
    });
    act(() => {
      pending = hook.result.current.turnOn({ analyse: false, fadeIn: true, remember: false });
    });
    await act(async () => Promise.resolve());
    expect(document.querySelector("audio").volume).toBe(0);
    resolveFade();
    await act(async () => expect(await pending).toBe(true));
    expect(analyser.getByteFrequencyData).toHaveBeenCalledTimes(1);
  });
  test("turning off cancels a pending start immediately", async () => {
    let resolvePlay;
    HTMLMediaElement.prototype.play.mockReturnValueOnce(
      new Promise((resolve) => {
        resolvePlay = resolve;
      })
    );
    const hook = renderHook(() => useRadio(), { wrapper });
    let pending;
    act(() => {
      pending = hook.result.current.turnOn({ analyse: false, remember: false });
    });
    expect(hook.result.current.isLoading).toBe(true);
    act(() => hook.result.current.turnOff({ remember: false }));
    same([hook.result.current.isLoading, false], [hook.result.current.isPlaying, false]);
    resolvePlay();
    await expect(pending).resolves.toBe(false);
  });
  test("animates startup volume and isolates preference persistence failures", async () => {
    const frames = [];
    vi.spyOn(performance, "now").mockReturnValue(1000);
    globalThis.requestAnimationFrame = vi.fn((callback) => {
      frames.push(callback);
      return frames.length;
    });
    mocks.updateUiPreferences.mockRejectedValueOnce(new Error("storage"));
    const hook = renderHook(() => useRadio(), { wrapper });
    act(() => hook.result.current.setVolume(0.6));
    await act(async () => {
      verify([await hook.result.current.turnOn({ fadeIn: true, analyse: false }), "toBe", true]);
    });
    const audio = document.querySelector("audio");
    act(() => frames.at(-1)(2000));
    verify([audio.volume, "toBeCloseTo", 0.3], [frames, "toHaveLength", 2]);
    act(() => frames.at(-1)(3000));
    verify([audio.volume, "toBeCloseTo", 0.6], [frames, "toHaveLength", 2], [cancelAnimationFrame, "toHaveBeenCalled"]);
  });
  test("clamps volume and switches valid stations", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    act(() => hook.result.current.setVolume(4));
    expect(hook.result.current.volume).toBe(1);
    verify([mocks.updateUiPreferences, "toHaveBeenLastCalledWith", "radio", expect.objectContaining({ volume: 1 })]);
    act(() => hook.result.current.setVolume("bad"));
    expect(hook.result.current.volume).toBe(0.1);
    act(() => hook.result.current.setStation("unknown"));
    expect(hook.result.current.stationId).toBe("poptron");
    mocks.updateUiPreferences.mockClear();
    HTMLMediaElement.prototype.load.mockClear();
    HTMLMediaElement.prototype.pause.mockClear();
    act(() => hook.result.current.setStation("poptron"));
    notCalled(mocks.updateUiPreferences, HTMLMediaElement.prototype.load, HTMLMediaElement.prototype.pause);
    await act(() => hook.result.current.turnOn());
    mocks.updateUiPreferences.mockClear();
    act(() => hook.result.current.setStation("indiepop"));
    await act(async () => Promise.resolve());
    same([hook.result.current.stationId, "indiepop"], [hook.result.current.station.id, "indiepop"]);
    verify([document.querySelector("audio").src, "toContain", "indiepop"], [hook.result.current.isPlaying, "toBe", true]);
    verify([mocks.updateUiPreferences, "toHaveBeenCalledWith", "radio", expect.objectContaining({ stationId: "indiepop" })]);
    expect(mocks.updateUiPreferences).toHaveBeenCalledTimes(1);
  });
  test("switching stations cancels an in-progress startup fade instead of leaking into the new station", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      await hook.result.current.turnOn({ fadeIn: true, analyse: false });
    });
    expect(requestAnimationFrame).toHaveBeenCalled();
    cancelAnimationFrame.mockClear();
    act(() => hook.result.current.setStation("indiepop"));
    // A fade cancels itself only when a NEW fade starts; switching stations
    // here doesn't necessarily begin one (turnOn defaults fadeIn to false),
    // so without an explicit cancel the stale rAF loop would keep nudging
    // the new station's volume toward the OLD fade's target for its
    // remaining duration -- heard as an unexplained dip-and-recover.
    expect(cancelAnimationFrame).toHaveBeenCalledWith(1);
  });
  test("resumes the selected station when switching during loading", async () => {
    let resolveOld;
    HTMLMediaElement.prototype.play.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      })
    );
    const hook = renderHook(() => useRadio(), { wrapper });
    let oldPlayback;
    act(() => {
      oldPlayback = hook.result.current.turnOn({ analyse: false });
    });
    await act(async () => Promise.resolve());
    expect(hook.result.current.isLoading).toBe(true);
    act(() => hook.result.current.setStation("indiepop"));
    await act(async () => Promise.resolve());
    same([hook.result.current.station.id, "indiepop"], [hook.result.current.isPlaying, true]);
    expect(document.querySelector("audio").src).toContain("indiepop");
    resolveOld();
    await expect(oldPlayback).resolves.toBe(false);
  });
  test("switches metadata without playback while recording is active", () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    fireEvent.error(document.querySelector("audio"));
    expect(hook.result.current.error).not.toBe("");
    act(() => hook.result.current.setRecordingActive(true));
    HTMLMediaElement.prototype.play.mockClear();
    mocks.updateUiPreferences.mockClear();
    act(() => hook.result.current.setStation("indiepop"));
    same([hook.result.current.station.id, "indiepop"], [hook.result.current.error, ""], [hook.result.current.isLoading, false]);
    verify([HTMLMediaElement.prototype.play, "not.toHaveBeenCalled"], [mocks.updateUiPreferences, "toHaveBeenCalledTimes", 1]);
    verify([mocks.updateUiPreferences, "toHaveBeenCalledWith", "radio", expect.objectContaining({ stationId: "indiepop" })]);
  });
  test("uses the latest station for a later manual start", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    act(() => hook.result.current.setStation("indiepop"));
    const audio = document.querySelector("audio");
    audio.removeAttribute("src");
    await act(() => hook.result.current.turnOn({ analyse: false, remember: false }));
    expect(audio.src).toContain("indiepop");
  });
  test("suspends radio while recording and resumes afterwards", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(() => hook.result.current.turnOn());
    mocks.updateUiPreferences.mockClear();
    act(() => hook.result.current.setRecordingActive(true));
    expect(hook.result.current.isPlaying).toBe(false);
    HTMLMediaElement.prototype.play.mockClear();
    act(() => hook.result.current.setRecordingActive(true));
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    await act(async () => {
      expect(await hook.result.current.turnOn()).toBe(false);
    });
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
    act(() => hook.result.current.setRecordingActive(false));
    await act(async () => Promise.resolve());
    verify([hook.result.current.isPlaying, "toBe", true], [mocks.updateUiPreferences, "not.toHaveBeenCalled"]);
    HTMLMediaElement.prototype.play.mockClear();
    act(() => hook.result.current.setRecordingActive(false));
    await act(async () => Promise.resolve());
    expect(HTMLMediaElement.prototype.play).not.toHaveBeenCalled();
  });
  test("ignores redundant recording-state notifications", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    mocks.updateUiPreferences.mockClear();
    HTMLMediaElement.prototype.pause.mockClear();
    act(() => hook.result.current.setRecordingActive(false));
    await act(async () => {
      verify([await hook.result.current.turnOn({ analyse: false, remember: false }), "toBe", true]);
    });
    expect(mocks.updateUiPreferences).not.toHaveBeenCalled();
    act(() => hook.result.current.turnOff({ remember: false }));
    HTMLMediaElement.prototype.pause.mockClear();
    act(() => hook.result.current.setRecordingActive(true));
    notCalled(HTMLMediaElement.prototype.pause, mocks.updateUiPreferences);
  });
  test("rejects a start before touching media while suspended", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    act(() => hook.result.current.setRecordingActive(true));
    HTMLMediaElement.prototype.load.mockClear();
    HTMLMediaElement.prototype.play.mockClear();
    await expect(hook.result.current.turnOn({ analyse: false, remember: false })).resolves.toBe(false);
    notCalled(HTMLMediaElement.prototype.load, HTMLMediaElement.prototype.play);
  });
  test("tries mirror streams and handles blocked startup autoplay", async () => {
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(new Error("first mirror failed")).mockResolvedValueOnce(undefined);
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      expect(await hook.result.current.turnOn()).toBe(true);
    });
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
    hook.result.current.turnOff({ remember: false });
    const blocked = new Error("user gesture is required");
    blocked.name = "NotAllowedError";
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(blocked);
    await act(async () => {
      verify([await hook.result.current.turnOn({ fadeIn: true, remember: false }), "toBe", false]);
    });
    same([hook.result.current.error, ""], [hook.result.current.isPlaying, false], [hook.result.current.isLoading, false]);
    window.dispatchEvent(new Event("keydown"));
    await act(async () => Promise.resolve());
    hook.result.current.turnOff({ remember: false });
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(new Error("user gesture required"));
    await act(async () => {
      verify([await hook.result.current.turnOn({ fadeIn: true, remember: false }), "toBe", false]);
    });
  });
  test("treats an autoplay rejection as a mirror failure without fade-in", async () => {
    const blocked = new Error("gesture required");
    blocked.name = "NotAllowedError";
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(blocked).mockResolvedValueOnce(undefined);
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      verify([await hook.result.current.turnOn({ analyse: false, fadeIn: false, remember: false }), "toBe", true]);
    });
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
    window.dispatchEvent(new Event("keydown"));
    await act(async () => Promise.resolve());
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
  });
  test("reports exhausted streams and recovers from media errors", async () => {
    const order = [];
    const immediate = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      order.push("delay");
      callback();
      return 1;
    });
    HTMLMediaElement.prototype.play.mockImplementation(() => {
      order.push("play");
      return Promise.reject(new Error("offline"));
    });
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      expect(await hook.result.current.turnOn()).toBe(false);
    });
    verify([hook.result.current.error, "toContain", "offline"], [hook.result.current.isPlaying, "toBe", false]);
    calledTimes([HTMLMediaElement.prototype.play, 4], [immediate, 1]);
    verify([immediate, "toHaveBeenCalledWith", expect.any(Function), 500], [order, "toEqual", ["play", "play", "delay", "play", "play"]]);
    immediate.mockRestore();
    HTMLMediaElement.prototype.play.mockResolvedValue(undefined);
    await act(() => hook.result.current.turnOn());
    mocks.updateUiPreferences.mockClear();
    HTMLMediaElement.prototype.play.mockClear();
    await act(async () => fireEvent.error(document.querySelector("audio")));
    verify([HTMLMediaElement.prototype.play, "toHaveBeenCalled"], [mocks.updateUiPreferences, "not.toHaveBeenCalled"]);
  });
  test("reports a media error before any playback attempt", () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    fireEvent.error(document.querySelector("audio"));
    verify([hook.result.current.error, "toContain", hook.result.current.station.name]);
    same([hook.result.current.isPlaying, false], [hook.result.current.isLoading, false]);
  });
  test("builds an analyser graph and clears its visual state", async () => {
    let constructions = 0;
    const { analyser, context } = installAudio({
      state: "suspended",
      getByteFrequencyData: vi.fn((data) => data.fill(200)),
      resume: vi.fn().mockResolvedValue(),
      close: vi.fn().mockRejectedValue(new Error("close failed")),
      onConstruct: () => constructions++
    });
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      expect(await hook.result.current.turnOn({ analyse: true })).toBe(true);
    });
    verify(
      [context.resume, "toHaveBeenCalled"],
      [analyser.getByteFrequencyData, "toHaveBeenCalledTimes", 1],
      [hook.result.current.getBassLevel(), "toBeGreaterThan", 0],
      [hook.result.current.getSpectrumLevels()[0], "toBeGreaterThan", 0]
    );
    verify([document.documentElement.style.getPropertyValue("--radio-analysis-active"), "toBe", "1"]);
    verify([document.documentElement.style.getPropertyValue("--radio-bass"), "toBe", hook.result.current.getBassLevel().toFixed(3)]);
    verify([
      document.documentElement.style.getPropertyValue("--radio-band-0"),
      "toBe",
      hook.result.current.getSpectrumLevels()[0].toFixed(3)
    ]);
    await act(() => hook.result.current.turnOn({ analyse: true }));
    verify([constructions, "toBe", 1], [analyser.getByteFrequencyData, "toHaveBeenCalledTimes", 2]);
    act(() => hook.result.current.turnOff({ remember: false }));
    verify([document.documentElement.style.getPropertyValue("--radio-analysis-active"), "toBe", "0"]);
    verify([document.documentElement.style.getPropertyValue("--radio-bass"), "toBe", "0"]);
    verify([document.documentElement.style.getPropertyValue("--radio-band-17"), "toBe", "0"]);
    expect(hook.result.current.getSpectrumLevels()).toEqual(Array(18).fill(0));
    const audio = document.querySelector("audio");
    HTMLMediaElement.prototype.load.mockClear();
    HTMLMediaElement.prototype.pause.mockClear();
    hook.unmount();
    verify([context.close, "toHaveBeenCalled"], [audio.hasAttribute("src"), "toBe", false]);
    calledTimes([HTMLMediaElement.prototype.pause, 1], [HTMLMediaElement.prototype.load, 1]);
  });
  test("handles a silent fade and a fade interrupted by pause", async () => {
    const frames = installFrameQueue();
    const hook = renderHook(() => useRadio(), { wrapper });
    act(() => hook.result.current.setVolume(0));
    const frameCount = frames.length;
    await act(() => hook.result.current.turnOn({ fadeIn: true }));
    const audio = document.querySelector("audio");
    verify([audio.volume, "toBe", 0], [frames, "toHaveLength", frameCount]);
    act(() => hook.result.current.setVolume(0.5));
    await act(() => hook.result.current.turnOn({ fadeIn: true }));
    audio.pause();
    const pausedFrameCount = frames.length;
    act(() => frames.at(-1)(performance.now() + 100));
    verify([audio.paused, "toBe", true], [frames, "toHaveLength", pausedFrameCount]);
  });
  test("cancels a stale fade whose audio element was detached", async () => {
    const frames = installFrameQueue();
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(() => hook.result.current.turnOn({ fadeIn: true, analyse: false }));
    const audio = document.querySelector("audio");
    const staleFrame = frames.at(-1);
    hook.unmount();
    Object.defineProperty(audio, "paused", { configurable: true, value: false });
    const frameCount = frames.length;
    act(() => staleFrame(performance.now() + 100));
    expect(frames).toHaveLength(frameCount);
  });
  test("stops analysis after a browser analyser failure", async () => {
    installAudio({
      getByteFrequencyData: vi.fn(() => {
        throw new Error("device removed");
      })
    });
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(() => hook.result.current.turnOn({ analyse: true }));
    verify([document.documentElement.style.getPropertyValue("--radio-analysis-active"), "toBe", "0"]);
  });
  test("cancels a pending playback and reports the final media-stream error", async () => {
    let resolvePlay;
    HTMLMediaElement.prototype.play.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePlay = resolve;
        })
    );
    const hook = renderHook(() => useRadio(), { wrapper });
    const pending = hook.result.current.turnOn();
    await act(async () => Promise.resolve());
    fireEvent.error(document.querySelector("audio"));
    expect(hook.result.current.error).toBe("");
    act(() => hook.result.current.turnOff({ remember: false }));
    resolvePlay();
    await expect(pending).resolves.toBe(false);
    HTMLMediaElement.prototype.play.mockResolvedValue(undefined);
    await act(() => hook.result.current.turnOn());
    fireEvent.error(document.querySelector("audio"));
    await act(async () => Promise.resolve());
    fireEvent.error(document.querySelector("audio"));
    await act(async () => Promise.resolve());
    expect(hook.result.current.error).not.toBe("");
  });
  test("reports a generic error when media rejects without a reason", async () => {
    const immediate = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      callback();
      return 1;
    });
    HTMLMediaElement.prototype.play.mockRejectedValue(undefined);
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      expect(await hook.result.current.turnOn()).toBe(false);
    });
    expect(hook.result.current.error).not.toBe("");
    verify([hook.result.current.error, "toContain", "No radio stream could be played"]);
    immediate.mockRestore();
  });
  test("recognizes message-only autoplay blocks and reasonless objects", async () => {
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(new Error("user gesture is required"));
    const blocked = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      expect(await blocked.result.current.turnOn({ fadeIn: true })).toBe(false);
    });
    blocked.unmount();
    const immediate = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      callback();
      return 1;
    });
    HTMLMediaElement.prototype.play.mockRejectedValue({});
    const reasonless = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      expect(await reasonless.result.current.turnOn()).toBe(false);
    });
    verify([reasonless.result.current.error, "not.toBe", ""], [immediate, "toHaveBeenCalledTimes", 1]);
    immediate.mockRestore();
    HTMLMediaElement.prototype.play.mockRejectedValueOnce("user gesture is required");
    const stringReason = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      verify([await stringReason.result.current.turnOn({ fadeIn: true }), "toBe", false]);
    });
    stringReason.unmount();
    const immediateNull = vi.spyOn(globalThis, "setTimeout").mockImplementation((callback) => {
      callback();
      return 1;
    });
    HTMLMediaElement.prototype.play.mockRejectedValue(null);
    const nullReason = renderHook(() => useRadio(), { wrapper });
    await act(async () => {
      verify([await nullReason.result.current.turnOn({ fadeIn: true }), "toBe", false]);
    });
    immediateNull.mockRestore();
  });
  test("unlocks a paused analyser and applies its decay response", async () => {
    const frames = installFrameQueue();
    let high = true;
    const { analyser, context } = installAudio({
      getByteFrequencyData: vi.fn((data) => data.fill(high ? 220 : 0))
    });
    const hook = renderHook(() => useRadio(), { wrapper });
    window.dispatchEvent(new Event("pointerdown"));
    await act(async () => Promise.resolve());
    expect(analyser.getByteFrequencyData).not.toHaveBeenCalled();
    await act(() => hook.result.current.turnOn({ analyse: true }));
    expect(context.resume).not.toHaveBeenCalled();
    const attack = hook.result.current.getBassLevel();
    high = false;
    act(() => frames.shift()(performance.now()));
    expect(hook.result.current.getBassLevel()).toBeLessThan(attack);
  });
  test("switches stations and recording suspension while stopped", async () => {
    const hook = renderHook(() => useRadio(), { wrapper });
    act(() => hook.result.current.setStation("indiepop"));
    expect(hook.result.current.stationId).toBe("indiepop");
    HTMLMediaElement.prototype.play.mockClear();
    act(() => hook.result.current.setRecordingActive(true));
    act(() => hook.result.current.setRecordingActive(false));
    await act(async () => Promise.resolve());
    verify([hook.result.current.isPlaying, "toBe", false], [HTMLMediaElement.prototype.play, "not.toHaveBeenCalled"]);
  });
  test("ignores late blocked and failed playback after unmount", async () => {
    let rejectBlocked;
    HTMLMediaElement.prototype.play.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectBlocked = reject;
      })
    );
    const blockedHook = renderHook(() => useRadio(), { wrapper });
    const blockedPlayback = blockedHook.result.current.turnOn({ fadeIn: true });
    await act(async () => Promise.resolve());
    blockedHook.unmount();
    const blocked = new Error("gesture required");
    blocked.name = "NotAllowedError";
    rejectBlocked(blocked);
    await expect(blockedPlayback).resolves.toBe(false);
    let rejectFailed;
    HTMLMediaElement.prototype.play.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectFailed = reject;
      })
    );
    const failedHook = renderHook(() => useRadio(), { wrapper });
    const failedPlayback = failedHook.result.current.turnOn();
    await act(async () => Promise.resolve());
    failedHook.unmount();
    rejectFailed(new Error("late"));
    await expect(failedPlayback).resolves.toBe(false);
  });
  test("cancels an older playback before it tries another mirror", async () => {
    let rejectFirst;
    HTMLMediaElement.prototype.play
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            rejectFirst = reject;
          })
      )
      .mockResolvedValue(undefined);
    const hook = renderHook(() => useRadio(), { wrapper });
    const older = hook.result.current.turnOn();
    await act(async () => Promise.resolve());
    const newer = hook.result.current.turnOn();
    await expect(newer).resolves.toBe(true);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
    rejectFirst(new Error("old mirror"));
    await expect(older).resolves.toBe(false);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
    expect(mocks.setDirectMonitorMediaActive).toHaveBeenLastCalledWith(true);
  });
  test("does not try another mirror after recording suspension", async () => {
    let rejectPlay;
    HTMLMediaElement.prototype.play.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectPlay = reject;
      })
    );
    const hook = renderHook(() => useRadio(), { wrapper });
    const pending = hook.result.current.turnOn({ analyse: false });
    await act(async () => Promise.resolve());
    act(() => hook.result.current.setRecordingActive(true));
    rejectPlay(new Error("offline"));
    await expect(pending).resolves.toBe(false);
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  });
  test("an obsolete playback cannot clear the newer loading state", async () => {
    let rejectOld;
    let resolveNew;
    HTMLMediaElement.prototype.play
      .mockReturnValueOnce(
        new Promise((_resolve, reject) => {
          rejectOld = reject;
        })
      )
      .mockReturnValueOnce(
        new Promise((resolve) => {
          resolveNew = resolve;
        })
      );
    const hook = renderHook(() => useRadio(), { wrapper });
    let oldPlayback;
    let newPlayback;
    act(() => {
      oldPlayback = hook.result.current.turnOn({ analyse: false });
    });
    act(() => {
      newPlayback = hook.result.current.turnOn({ analyse: false });
    });
    rejectOld(new Error("obsolete"));
    await act(async () => expect(await oldPlayback).toBe(false));
    expect(hook.result.current.isLoading).toBe(true);
    resolveNew();
    await act(async () => expect(await newPlayback).toBe(true));
    expect(hook.result.current.isLoading).toBe(false);
  });
  test("ignores an analyser frame after analysis has stopped", async () => {
    const frames = installFrameQueue();
    const { analyser } = installAudio({
      getByteFrequencyData: vi.fn((data) => data.fill(100))
    });
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(() => hook.result.current.turnOn({ analyse: true }));
    const staleFrame = frames.at(-1);
    act(() => hook.result.current.turnOff({ remember: false }));
    act(() => staleFrame(performance.now()));
    expect(analyser.getByteFrequencyData).toHaveBeenCalledTimes(1);
  });
  test("rebuilds a closed analyser graph and ignores its stale frame", async () => {
    const frames = installFrameQueue();
    const contexts = [];
    window.AudioContext = class {
      constructor() {
        this.state = "running";
        this.sampleRate = 48000;
        this.destination = {};
        this.analyser = {
          fftSize: 0,
          smoothingTimeConstant: 0,
          frequencyBinCount: 32,
          connect: vi.fn(),
          getByteFrequencyData: vi.fn((data) => data.fill(120))
        };
        this.createAnalyser = vi.fn(() => this.analyser);
        this.createMediaElementSource = vi.fn(() => ({ connect: vi.fn() }));
        this.resume = vi.fn();
        this.close = vi.fn().mockResolvedValue();
        contexts.push(this);
      }
    };
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(() => hook.result.current.turnOn({ analyse: true }));
    const staleFrame = frames.at(-1);
    contexts[0].state = "closed";
    act(() => staleFrame(performance.now()));
    expect(contexts[0].analyser.getByteFrequencyData).toHaveBeenCalledTimes(1);
    document.querySelector("audio").pause();
    window.dispatchEvent(new Event("pointerdown"));
    await act(async () => Promise.resolve());
    expect(contexts).toHaveLength(2);
    contexts[0].state = "running";
    act(() => staleFrame(performance.now()));
    expect(contexts[0].analyser.getByteFrequencyData).toHaveBeenCalledTimes(1);
  });
  test("retries startup autoplay on the first user gesture", async () => {
    store({ stationId: "poptron", volume: 0.45, enabled: true });
    const blocked = new Error("gesture required");
    blocked.name = "NotAllowedError";
    let resolveRetry;
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(blocked).mockImplementationOnce(function retryPending() {
      Object.defineProperty(this, "paused", { configurable: true, value: false });
      return new Promise((resolve) => {
        resolveRetry = resolve;
      });
    });
    const { analyser } = installAudio();
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => Promise.resolve());
    window.dispatchEvent(new Event("keydown"));
    await act(async () => Promise.resolve());
    expect(document.querySelector("audio").volume).toBe(0);
    window.dispatchEvent(new Event("keydown"));
    await act(async () => Promise.resolve());
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(2);
    resolveRetry();
    await act(async () => Promise.resolve());
    expect(hook.result.current.isPlaying).toBe(true);
    calledTimes([HTMLMediaElement.prototype.play, 2], [analyser.getByteFrequencyData, 2]);
    window.dispatchEvent(new Event("keydown"));
    await act(async () => Promise.resolve());
    verify([HTMLMediaElement.prototype.play, "toHaveBeenCalledTimes", 2], [mocks.updateUiPreferences, "not.toHaveBeenCalled"]);
  });
  test("starts enabled radio with analysis but without rewriting preferences", async () => {
    store({ stationId: "poptron", volume: 0.45, enabled: true });
    const { analyser } = installAudio();
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => Promise.resolve());
    same([hook.result.current.isPlaying, true], [document.querySelector("audio").volume, 0]);
    verify([analyser.getByteFrequencyData, "toHaveBeenCalled"], [mocks.updateUiPreferences, "not.toHaveBeenCalled"]);
  });
  test("removes the keydown autoplay listener on unmount", async () => {
    store({ stationId: "poptron", volume: 0.45, enabled: true });
    const blocked = new Error("gesture required");
    blocked.name = "NotAllowedError";
    HTMLMediaElement.prototype.play.mockRejectedValueOnce(blocked);
    const hook = renderHook(() => useRadio(), { wrapper });
    await act(async () => Promise.resolve());
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
    const removeListener = vi.spyOn(window, "removeEventListener");
    hook.unmount();
    verify([removeListener, "toHaveBeenCalledWith", "keydown", expect.any(Function), true]);
    window.dispatchEvent(new Event("keydown"));
    await act(async () => Promise.resolve());
    expect(HTMLMediaElement.prototype.play).toHaveBeenCalledTimes(1);
  });
});
