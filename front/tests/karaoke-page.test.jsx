/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { same, called, calledWith, verify } from "./helpers/assertions.mjs";
const mocks = vi.hoisted(() => ({
  location: { state: { songId: "song" } },
  navigate: vi.fn(),
  songsPoll: { data: [] },
  listSongs: vi.fn(),
  getSong: vi.fn(),
  result: { result: null, loading: false, error: null },
  room: {
    room: null,
    participants: [],
    roomUi: {},
    syncUi: vi.fn(),
    setLocalMonitoring: vi.fn().mockResolvedValue(false)
  },
  radio: {
    isPlaying: false,
    setRecordingActive: vi.fn(),
    toggle: vi.fn(),
    turnOff: vi.fn(),
    turnOn: vi.fn().mockResolvedValue(undefined)
  },
  preferences: null,
  transport: null,
  transportOptions: null,
  controls: null,
  microphone: null,
  consoleProps: null,
  mediaProps: null,
  mediaSyncOptions: null,
  renderMedia: true,
  stageProps: null,
  startMonitoring: vi.fn(),
  stopMonitoring: vi.fn()
}));
vi.mock("react-router-dom", () => ({
  useLocation: () => mocks.location,
  useNavigate: () => mocks.navigate
}));
vi.mock("../src/contexts/OnlineRoomContext", () => ({ useOnlineRoom: () => mocks.room }));
vi.mock("../src/contexts/radio", () => ({ useRadio: () => mocks.radio }));
vi.mock("../src/hooks/usePolling", () => ({
  usePolling: (request) => {
    if (request === mocks.listSongs) return mocks.songsPoll;
    request();
    return { data: null, error: null };
  }
}));
vi.mock("../src/api/client", () => ({
  api: {
    listSongs: mocks.listSongs,
    getSong: mocks.getSong,
    listAudioOutputDevices: vi.fn(),
    getAudioSettings: vi.fn(),
    getSignalQuality: vi.fn(),
    startDirectMonitoring: mocks.startMonitoring,
    stopDirectMonitoring: mocks.stopMonitoring
  }
}));
vi.mock("../src/pages/Karaoke/media", () => ({
  default: (props) => {
    mocks.mediaProps = props;
    if (!mocks.renderMedia) return <div data-testid="media" />;
    return (
      <>
        <audio ref={props.instrumentalRef} />
        <audio ref={props.vocalsRef} />
      </>
    );
  }
}));
vi.mock("../src/pages/Karaoke/performance-stage", () => ({
  default: (props) => {
    mocks.stageProps = props;
    return <div data-testid="stage" />;
  }
}));
vi.mock("../src/pages/Karaoke/console", () => ({
  default: (props) => {
    mocks.consoleProps = props;
    return (
      <div data-testid="console">
        <button data-testid="play" onClick={props.onTogglePlay} />
        <button data-testid="stop" onClick={props.onStop} />
        <button data-testid="monitor" onClick={() => props.onMonitoringChange(true)} />
        <button data-testid="preset" onClick={() => props.onApplyEffectPreset({ id: "hall", reverb: 0.4, echo: 0.2, delay: 0.1 })} />
        <button data-testid="monitor-off" onClick={() => props.onMonitoringChange(false)} />
        <button data-testid="effect" onClick={() => props.onEffectChange("echo", 0.6)} />
        <button data-testid="effect-commit" onClick={() => props.onEffectCommit("echo", 0.6)} />
        <button data-testid="commit" onClick={() => props.onVolumeCommit.microphone(0.9)} />
        <button data-testid="tempo" onClick={() => props.onTempoChange(-200)} />
        <button data-testid="lyrics-offset" onClick={() => props.onLyricsOffsetChange(-4)} />
        <button data-testid="notes" onClick={props.onToggleNotes} />
        <button data-testid="lyrics" onClick={props.onToggleLyrics} />
        <button data-testid="auto-hide" onClick={() => props.onAutoHideChange(true)} />
        <button data-testid="seek" onClick={() => props.onSeek(2)} />
        <button data-testid="skip" onClick={() => props.onSkip(5)} />
      </div>
    );
  }
}));
vi.mock("../src/components/PerformanceAnalysisModal", () => ({
  default: (props) => (
    <div data-testid="analysis-modal">
      <button data-testid="analysis-close" onClick={props.onClose} />
      <button data-testid="analysis-done" onClick={props.onDone} />
      <button data-testid="analysis-delete" onClick={props.onDeleted} />
    </div>
  )
}));
vi.mock("../src/pages/Karaoke/hooks/useKaraokeResult", () => ({ default: () => mocks.result }));
vi.mock("../src/pages/Karaoke/hooks/useKaraokePreferences", () => ({
  default: () => mocks.preferences
}));
vi.mock("../src/pages/Karaoke/hooks/useKaraokeControls", () => ({ default: () => mocks.controls }));
vi.mock("../src/pages/Karaoke/hooks/useMicrophoneSettings", () => ({
  default: () => mocks.microphone
}));
vi.mock("../src/pages/Karaoke/hooks/useAudioOutputRouting", () => ({ default: vi.fn() }));
vi.mock("../src/pages/Karaoke/hooks/useKaraokeMediaSync", () => ({
  default: (options) => {
    mocks.mediaSyncOptions = options;
    return { sendYouTubeCommand: vi.fn(), syncSecondaryMedia: vi.fn() };
  }
}));
vi.mock("../src/pages/Karaoke/performance-stage/usePitchDetection", () => ({
  default: () => ({
    sungMidi: 60,
    isPitchDetected: true,
    isPitchAttacking: false,
    pitchRestProgress: 0
  })
}));
vi.mock("../src/pages/Karaoke/hooks/useKaraokeTransport", () => ({
  default: (options) => {
    mocks.transportOptions = options;
    return mocks.transport;
  }
}));
vi.mock("../src/pages/Karaoke/hooks/useMelodyGuide", () => ({
  default: () => ({
    startMelodyGuide: vi.fn(),
    updateMelodyGuide: vi.fn(),
    silenceMelodyGuide: vi.fn()
  })
}));
vi.mock("../src/pages/Karaoke/hooks/useKaraokeHotkeys", () => ({ default: vi.fn() }));
vi.mock("../src/pages/Karaoke/hooks/useKaraokeStageLayout", () => ({ default: vi.fn() }));
import Karaoke from "../src/pages/Karaoke/index.jsx";
const song = {
  id: "song",
  title: "Song",
  artist: "Artist",
  status: "done",
  key_override: "C",
  note_range_min: 50,
  note_range_max: 75
};
const result = {
  lyrics_sync: {
    bpm: 120,
    key: "C",
    words: [{ text: "Line", start: 0, end: 1, notes: [{ note: 60, start: 0, end: 1 }] }]
  }
};
beforeEach(() => {
  Object.defineProperties(HTMLMediaElement.prototype, {
    load: { configurable: true, value: vi.fn() },
    readyState: { configurable: true, get: () => 0 }
  });
  mocks.location = { state: { songId: "song" } };
  mocks.navigate.mockReset();
  mocks.songsPoll = { data: [song], error: null };
  mocks.getSong.mockReset().mockResolvedValue(song);
  mocks.result = { result, loading: false, error: null };
  mocks.renderMedia = true;
  mocks.room.room = null;
  mocks.room.participants = [];
  mocks.room.syncUi.mockReset();
  mocks.room.setLocalMonitoring.mockReset().mockResolvedValue(false);
  mocks.radio.isPlaying = false;
  Object.values(mocks.radio).forEach((value) => value?.mockClear?.());
  mocks.preferences = {
    musicVolume: 0.7,
    setMusicVolume: vi.fn(),
    vocalVolume: 0.4,
    setVocalVolume: vi.fn(),
    melodyVolume: 0.8,
    setMelodyVolume: vi.fn(),
    speed: 1,
    setSpeed: vi.fn(),
    keyShift: 0,
    setKeyShift: vi.fn(),
    showLyrics: true,
    setShowLyrics: vi.fn(),
    showNotes: true,
    setShowNotes: vi.fn(),
    autoHideConsole: false,
    setAutoHideConsole: vi.fn(),
    effectPreset: "studio",
    setEffectPreset: vi.fn(),
    timingOffsets: {},
    setTimingOffsets: vi.fn()
  };
  mocks.controls = {
    controlsVisible: true,
    hideControls: vi.fn(),
    revealControls: vi.fn(),
    showControls: vi.fn()
  };
  mocks.microphone = {
    microphoneVolume: 0.5,
    setMicrophoneVolume: vi.fn(),
    microphoneEffects: { reverb: 0, echo: 0, delay: 0 },
    setMicrophoneEffects: vi.fn((updater) => updater({ reverb: 0, echo: 0, delay: 0 })),
    audioDriver: "wasapi",
    directOutputDeviceId: null,
    setDirectOutputDeviceId: vi.fn(),
    monitoringEnabled: false,
    setMonitoringEnabled: vi.fn(),
    monitorInputDeviceId: null,
    updateMicrophone: vi.fn(),
    updateMicrophoneEffects: vi.fn().mockResolvedValue({})
  };
  mocks.transport = {
    returnToLibrary: vi.fn(),
    seekTo: vi.fn(),
    skip: vi.fn(),
    stop: vi.fn().mockResolvedValue(true),
    togglePlay: vi.fn().mockResolvedValue(true)
  };
  mocks.startMonitoring.mockReset().mockResolvedValue({ monitoring_enabled: true });
  mocks.stopMonitoring.mockReset().mockResolvedValue({ monitoring_enabled: false });
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
describe("karaoke page", () => {
  test("renders ready song and wires stage, console, radio and monitoring", async () => {
    const appSettings = vi.fn();
    const page = render(<Karaoke onOpenAppSettings={appSettings} />);
    expect(page.getByTestId("stage")).not.toBeNull();
    same([mocks.stageProps.songId, "song"], [mocks.consoleProps.currentTempo, 120]);
    same([mocks.consoleProps.lyricsOffset, 0], [mocks.stageProps.currentTime, 0]);
    fireEvent.mouseMove(page.container.querySelector('[data-role="karaoke"]'));
    fireEvent.click(page.getByTestId("preset"));
    verify([mocks.preferences.setEffectPreset, "toHaveBeenCalledWith", "hall"], [mocks.microphone.updateMicrophoneEffects, "toHaveBeenCalled"]);
    fireEvent.click(page.getByTestId("monitor"));
    await waitFor(() => expect(mocks.startMonitoring).toHaveBeenCalled());
    expect(mocks.microphone.setMonitoringEnabled).toHaveBeenCalledWith(true);
    const radio = page.container.querySelector('[data-action="radio"]');
    fireEvent.click(radio);
    expect(mocks.radio.toggle).toHaveBeenCalled();
  });
  test("covers library, song, processing and result guard states", () => {
    const cases = [
      [{ data: null, error: new Error("offline") }, mocks.result, "[role=alert]"],
      [{ data: null, error: {} }, mocks.result, "[role=alert]"],
      [{ data: null, error: null }, mocks.result, "[role=status]"],
      [{ data: [], error: null }, mocks.result, "[role=status]"],
      [{ data: [{ ...song, status: "processing" }], error: null }, mocks.result, "[role=status]"],
      [{ data: [song], error: null }, { result, loading: true, error: null }, "[role=status]"],
      [{ data: [song], error: null }, { result: null, loading: false, error: new Error("bad") }, "[role=alert]"],
      [{ data: [song], error: null }, { result: null, loading: false, error: null }, "[role=alert]"]
    ];
    for (const [poll, karaokeResult, selector] of cases) {
      mocks.songsPoll = poll;
      mocks.result = karaokeResult;
      const view = render(<Karaoke />);
      expect(view.container.querySelector(selector)).not.toBeNull();
      cleanup();
    }
  });
  test("applies and saves a per-song lyrics offset without moving media time", () => {
    const timingKey = "song|120||0";
    mocks.preferences.timingOffsets = { [timingKey]: -3 };
    const page = render(<Karaoke />);
    same([mocks.stageProps.currentTime, 0], [mocks.consoleProps.lyricsOffset, -3]);
    same([mocks.stageProps.lyricsSync.words[0].start, -3], [mocks.stageProps.notes[0].start, -3]);
    mocks.mediaSyncOptions.currentTimeRef.current = 5;
    same([mocks.stageProps.currentTimeRef.current, 5], [mocks.mediaSyncOptions.currentTimeRef.current, 5]);
    fireEvent.click(page.getByTestId("lyrics-offset"));
    expect(mocks.preferences.setTimingOffsets).toHaveBeenCalledWith({ [timingKey]: -4 });
  });
  test("does not apply an embedded manual alignment twice", () => {
    mocks.result.result = {
      ...result,
      lyrics_sync: {
        ...result.lyrics_sync,
        alignment: { offset_seconds: -3.029 }
      }
    };
    render(<Karaoke />);
    same([mocks.consoleProps.lyricsOffset, -3.029], [mocks.stageProps.currentTime, 0]);
    same([mocks.stageProps.lyricsSync.words[0].start, 0], [mocks.stageProps.notes[0].start, 0]);
    mocks.mediaSyncOptions.currentTimeRef.current = 5;
    expect(mocks.stageProps.currentTimeRef.current).toBe(5);
  });
  test("selects the first ready song when route state has no song id", () => {
    mocks.location = { state: null };
    mocks.songsPoll = {
      data: [{ ...song, id: "pending", status: "processing" }, song],
      error: null
    };
    render(<Karaoke />);
    expect(mocks.stageProps.songId).toBe("song");
  });
  test("loads an explicitly routed transferred song when the shared list cache is stale", async () => {
    const transferred = { ...song, id: "host-local-song", title: "Transferred" };
    mocks.location = { state: { songId: transferred.id, autoPlay: true } };
    mocks.songsPoll = { data: [song], error: null };
    mocks.getSong.mockResolvedValueOnce(transferred);

    const page = render(<Karaoke />);

    await waitFor(() => expect(mocks.getSong).toHaveBeenCalledWith(transferred.id));
    await waitFor(() => expect(mocks.stageProps.songId).toBe(transferred.id));
    expect(page.container.querySelector('[data-role="karaoke"]')).not.toBeNull();
  });
  test("renders the no-ready-song message without route state", () => {
    mocks.location = { state: null };
    mocks.songsPoll = { data: null, error: null };
    const loading = render(<Karaoke />);
    expect(loading.container.querySelector("[role=status]")).not.toBeNull();
    loading.unmount();
    mocks.songsPoll = { data: [], error: null };
    const empty = render(<Karaoke />);
    expect(empty.container.querySelector("[role=status]")).not.toBeNull();
  });
  test("starts playback with intro and stops through blackout transition", async () => {
    vi.useFakeTimers();
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("play"));
    fireEvent.mouseMove(page.container.querySelector('[data-role="karaoke"]'));
    fireEvent.click(page.getByTestId("play"));
    await act(async () => Promise.resolve());
    page.container.querySelectorAll("audio").forEach((audio) => fireEvent.canPlay(audio));
    await vi.runAllTimersAsync();
    expect(mocks.transport.togglePlay).toHaveBeenCalledWith({ forcePlaying: true });
    fireEvent.click(page.getByTestId("stop"));
    fireEvent.click(page.getByTestId("stop"));
    await vi.runAllTimersAsync();
    expect(mocks.transport.stop).toHaveBeenCalled();
    expect(mocks.transport.returnToLibrary).toHaveBeenCalledWith({
      alreadyStopped: true,
      analysisId: null
    });
  });
  test("syncs participant effects while a room is active", () => {
    mocks.room.room = { host: true };
    mocks.room.participants = [{ id: "guest" }];
    render(<Karaoke />);
    verify([
      mocks.room.syncUi,
      "toHaveBeenCalledWith",
      {
        participantEffects: {
          volume: mocks.microphone.microphoneVolume,
          ...mocks.microphone.microphoneEffects
        }
      }
    ]);
  });
  test("suspends radio only for an active recording during playback", async () => {
    render(<Karaoke />);
    await act(async () => mocks.transportOptions.setRecordingSessionId("rec"));
    expect(mocks.radio.setRecordingActive).toHaveBeenLastCalledWith(false);
    await act(async () => mocks.transportOptions.setIsPlaying(true));
    expect(mocks.radio.setRecordingActive).toHaveBeenLastCalledWith(true);
    await act(async () => mocks.transportOptions.setRecordingSessionId(null));
    expect(mocks.radio.setRecordingActive).toHaveBeenLastCalledWith(false);
  });
  test("reports direct monitoring failure", async () => {
    mocks.startMonitoring.mockRejectedValueOnce(new Error("monitor failed"));
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("monitor"));
    await waitFor(() => expect(page.container.querySelector("[role=alert]").textContent).toContain("monitor failed"));
  });
  test("uses the low-latency room stream while an online room is active", async () => {
    mocks.room.room = { host: true };
    mocks.room.setLocalMonitoring.mockResolvedValueOnce(true);
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("monitor"));
    await waitFor(() =>
      expect(mocks.room.setLocalMonitoring).toHaveBeenCalledWith(true, {
        volume: 0.5,
        reverb: 0,
        echo: 0,
        delay: 0
      })
    );
    expect(mocks.startMonitoring).not.toHaveBeenCalled();
    expect(mocks.consoleProps.monitoringEnabled).toBe(true);
  });
  test("wires all console mutations and stopping monitoring", async () => {
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("effect"));
    fireEvent.click(page.getByTestId("effect-commit"));
    fireEvent.click(page.getByTestId("commit"));
    fireEvent.click(page.getByTestId("tempo"));
    fireEvent.click(page.getByTestId("notes"));
    fireEvent.click(page.getByTestId("lyrics"));
    fireEvent.click(page.getByTestId("auto-hide"));
    fireEvent.click(page.getByTestId("seek"));
    fireEvent.click(page.getByTestId("skip"));
    fireEvent.click(page.getByTestId("monitor-off"));
    await waitFor(() => expect(mocks.stopMonitoring).toHaveBeenCalled());
    calledWith(
      [mocks.preferences.setEffectPreset, ["custom"]],
      [mocks.microphone.updateMicrophoneEffects, [{ echo: 0.6 }]],
      [mocks.microphone.updateMicrophone, [{ volume: 0.9 }]],
      [mocks.preferences.setSpeed, [0.5]]
    );
    called(mocks.preferences.setShowNotes, mocks.preferences.setShowLyrics);
    calledWith(
      [mocks.preferences.setAutoHideConsole, [true]],
      [mocks.transport.seekTo, [2]],
      [mocks.transport.skip, [5]],
      [mocks.microphone.setMonitoringEnabled, [false]]
    );
  });
  test("rolls an optimistic effect preset back when persistence fails", async () => {
    mocks.microphone.updateMicrophoneEffects.mockResolvedValueOnce(null);
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("preset"));
    await waitFor(() =>
      expect(mocks.preferences.setEffectPreset).toHaveBeenLastCalledWith("studio")
    );
  });
  test("opens analysis result and handles every completion path", async () => {
    const page = render(<Karaoke />);
    await act(async () => mocks.transportOptions.setAnalysisRecordingId("rec"));
    expect(page.getByTestId("analysis-modal")).toBeTruthy();
    fireEvent.click(page.getByTestId("analysis-close"));
    verify([mocks.navigate, "toHaveBeenCalledWith", "/", expect.objectContaining({ replace: true })]);
    await act(async () => mocks.transportOptions.setAnalysisRecordingId((previous) => previous || "next"));
    fireEvent.click(page.getByTestId("analysis-done"));
    await act(async () => mocks.transportOptions.setAnalysisRecordingId("last"));
    fireEvent.click(page.getByTestId("analysis-delete"));
    expect(mocks.navigate).toHaveBeenCalledTimes(3);
  });
  test("pauses, resumes and restores radio after the first intro", async () => {
    vi.useFakeTimers();
    mocks.radio.isPlaying = true;
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("play"));
    await vi.runAllTimersAsync();
    await act(async () => mocks.transportOptions.setIsPlaying(true));
    mocks.radio.turnOn.mockRejectedValueOnce(new Error("radio unavailable"));
    fireEvent.click(page.getByTestId("play"));
    await act(async () => Promise.resolve());
    calledWith([mocks.transport.togglePlay, [{ forcePlaying: false }]], [mocks.radio.turnOn, [{ remember: false, fadeIn: true }]]);
    await act(async () => mocks.transportOptions.setIsPlaying(false));
    fireEvent.click(page.getByTestId("play"));
    verify([mocks.radio.turnOff, "toHaveBeenCalled"], [mocks.transport.togglePlay, "toHaveBeenCalledWith", { forcePlaying: true }]);
  });
  test("auto-starts after route handoff and clears its timers on unmount", async () => {
    vi.useFakeTimers();
    Object.defineProperty(HTMLMediaElement.prototype, "readyState", {
      configurable: true,
      get: () => 4
    });
    mocks.location = { state: { songId: "song", autoPlay: true } };
    const events = [];
    const listener = (event) => events.push(event.detail.visible);
    window.addEventListener("app:route-blackout", listener);
    const page = render(<Karaoke />);
    await vi.runAllTimersAsync();
    verify([mocks.transport.togglePlay, "toHaveBeenCalledWith", { forcePlaying: true }], [events, "toContain", false]);
    page.unmount();
    window.removeEventListener("app:route-blackout", listener);
  });
  test("releases blackout when auto-start media never mounts", async () => {
    vi.useFakeTimers();
    mocks.location = { state: { songId: "song", autoPlay: true } };
    mocks.renderMedia = false;
    const page = render(<Karaoke />);
    await vi.runAllTimersAsync();
    verify([mocks.transport.togglePlay, "not.toHaveBeenCalled"], [mocks.stageProps.sceneBlackout, "toBe", false]);
    page.unmount();
  });
  test("continues intro after media readiness timeout and handles media-ended callback", async () => {
    vi.useFakeTimers();
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("play"));
    await vi.runAllTimersAsync();
    expect(mocks.transport.togglePlay).toHaveBeenCalledWith({ forcePlaying: true });
    const ended = mocks.mediaSyncOptions.onPlaybackEndedRef.current();
    await vi.runAllTimersAsync();
    await ended;
    expect(mocks.transport.stop).toHaveBeenCalled();
  });
  test("starts immediately when song media is already ready", async () => {
    vi.useFakeTimers();
    Object.defineProperty(HTMLMediaElement.prototype, "readyState", {
      configurable: true,
      get: () => 4
    });
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("play"));
    await vi.runAllTimersAsync();
    expect(mocks.transport.togglePlay).toHaveBeenCalledWith({ forcePlaying: true });
  });
  test("handles declined playback, pause and stop transitions", async () => {
    vi.useFakeTimers();
    mocks.radio.isPlaying = true;
    mocks.transport.togglePlay.mockResolvedValue(false);
    mocks.transport.stop.mockResolvedValue(false);
    const page = render(<Karaoke />);
    fireEvent.click(page.getByTestId("play"));
    await vi.runAllTimersAsync();
    await act(async () => mocks.transportOptions.setIsPlaying(true));
    fireEvent.click(page.getByTestId("play"));
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("stop"));
    await vi.runAllTimersAsync();
    expect(mocks.transport.stop).toHaveBeenCalled();
  });
  test("uses only lyricsSync tempo and key while controls are hidden", () => {
    mocks.songsPoll = { data: [{ ...song, key_override: "F#", tempo_override: 42 }], error: null };
    mocks.result = {
      result: { ...result, lyrics_sync: { ...result.lyrics_sync, bpm: 137, key: "D" } },
      loading: false,
      error: null
    };
    mocks.controls.controlsVisible = false;
    const page = render(<Karaoke />);
    verify([mocks.consoleProps.currentTempo, "toBe", 137], [mocks.consoleProps.compactKey, "toContain", "D"]);
    // Stage actions render in a fixed order: back, console visibility
    // toggle, radio.
    const consoleToggle = page.container.querySelector('[data-action="console"]');
    fireEvent.click(consoleToggle);
    expect(mocks.controls.showControls).toHaveBeenCalled();
  });
  test("ignores an auto-start callback retained after unmount", () => {
    mocks.location = { state: { songId: "song", autoPlay: true } };
    let autoStart;
    const nativeSetTimeout = window.setTimeout;
    vi.spyOn(window, "setTimeout").mockImplementation((callback, timeout, ...args) => {
      if (timeout === 80) autoStart = callback;
      return nativeSetTimeout(callback, timeout, ...args);
    });
    const page = render(<Karaoke />);
    page.unmount();
    expect(() => autoStart()).not.toThrow();
  });
});
