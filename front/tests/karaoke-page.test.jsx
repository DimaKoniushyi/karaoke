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
  getResult: vi.fn(),
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
    getResult: mocks.getResult,
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
        <button data-testid="monitor" onClick={() => props.audio.onMonitoringChange(true)} />
        <button data-testid="preset" onClick={() => props.audio.onApplyEffectPreset({ id: "hall", reverb: 0.4, echo: 0.2, delay: 0.1 })} />
        <button data-testid="monitor-off" onClick={() => props.audio.onMonitoringChange(false)} />
        <button data-testid="effect" onClick={() => props.audio.onEffectChange("echo", 0.6)} />
        <button data-testid="effect-commit" onClick={() => props.audio.onEffectCommit("echo", 0.6)} />
        <button data-testid="tempo" onClick={() => props.timeline.changeTempo(-200)} />
        <button data-testid="lyrics-offset" onClick={() => props.timeline.changeLyricsOffset(-4)} />
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
vi.mock("../src/pages/Karaoke/hooks/useKaraokePreferences", () => ({
  default: () => mocks.preferences
}));
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
  mocks.getResult.mockReset().mockResolvedValue(result);
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
    togglePlay: vi.fn().mockResolvedValue(true),
    recordingSessionId: null,
    recordingError: null,
    analysisRecordingId: null,
    analysisRecordingIdRef: { current: null },
    clearAnalysis: vi.fn()
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
    await act(async () => Promise.resolve());
    expect(page.getByTestId("stage")).not.toBeNull();
    same([mocks.stageProps.songId, "song"], [mocks.consoleProps.timeline.currentTempo, 120]);
    same([mocks.consoleProps.timeline.lyricsOffset, 0], [mocks.stageProps.currentTime, 0]);
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
  test("covers library, song, processing and result guard states", async () => {
    // The third column configures how the mocked getResult() behaves for
    // that case; null keeps the default resolved value from beforeEach
    // (irrelevant for the first five rows, since the guard above the result
    // check already rejects those before the song is ever "done").
    const cases = [
      [{ data: null, error: new Error("offline") }, null, "[role=alert]"],
      [{ data: null, error: {} }, null, "[role=alert]"],
      [{ data: null, error: null }, null, "[role=status]"],
      [{ data: [], error: null }, null, "[role=status]"],
      [{ data: [{ ...song, status: "processing" }], error: null }, null, "[role=status]"],
      [{ data: [song], error: null }, () => new Promise(() => {}), "[role=status]"],
      [{ data: [song], error: null }, () => Promise.reject(new Error("bad")), "[role=alert]"],
      [{ data: [song], error: null }, () => Promise.resolve(null), "[role=alert]"]
    ];
    for (const [poll, configureResult, selector] of cases) {
      mocks.songsPoll = poll;
      if (configureResult) mocks.getResult.mockImplementationOnce(configureResult);
      const view = render(<Karaoke />);
      await act(async () => Promise.resolve());
      expect(view.container.querySelector(selector)).not.toBeNull();
      cleanup();
    }
  });
  test("applies and saves a per-song lyrics offset without moving media time", async () => {
    const timingKey = "song|120||0";
    mocks.preferences.timingOffsets = { [timingKey]: -3 };
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    same([mocks.stageProps.currentTime, 0], [mocks.consoleProps.timeline.lyricsOffset, -3]);
    same([mocks.stageProps.lyricsSync.words[0].start, -3], [mocks.stageProps.notes[0].start, -3]);
    mocks.mediaSyncOptions.currentTimeRef.current = 5;
    same([mocks.stageProps.currentTimeRef.current, 5], [mocks.mediaSyncOptions.currentTimeRef.current, 5]);
    fireEvent.click(page.getByTestId("lyrics-offset"));
    expect(mocks.preferences.setTimingOffsets).toHaveBeenCalledWith({ [timingKey]: -4 });
  });
  test("does not apply an embedded manual alignment twice", async () => {
    mocks.getResult.mockResolvedValueOnce({
      ...result,
      lyrics_sync: {
        ...result.lyrics_sync,
        alignment: { offset_seconds: -3.029 }
      }
    });
    render(<Karaoke />);
    await act(async () => Promise.resolve());
    same([mocks.consoleProps.timeline.lyricsOffset, -3.029], [mocks.stageProps.currentTime, 0]);
    same([mocks.stageProps.lyricsSync.words[0].start, 0], [mocks.stageProps.notes[0].start, 0]);
    mocks.mediaSyncOptions.currentTimeRef.current = 5;
    expect(mocks.stageProps.currentTimeRef.current).toBe(5);
  });
  test("selects the first ready song when route state has no song id", async () => {
    mocks.location = {};
    mocks.songsPoll = {
      data: [{ ...song, id: "pending", status: "processing" }, song],
      error: null
    };
    render(<Karaoke />);
    await act(async () => Promise.resolve());
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
    mocks.location = {};
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
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("play"));
    fireEvent.mouseMove(page.container.querySelector('[data-role="karaoke"]'));
    fireEvent.click(page.getByTestId("play"));
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
  test("syncs participant effects while a room is active", async () => {
    mocks.room.room = { host: true };
    mocks.room.participants = [{ id: "guest" }];
    render(<Karaoke />);
    await act(async () => Promise.resolve());
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
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    mocks.transport.recordingSessionId = "rec";
    page.rerender(<Karaoke />);
    expect(mocks.radio.setRecordingActive).toHaveBeenLastCalledWith(false);
    await act(async () => mocks.transportOptions.playback.setPlaying(true));
    expect(mocks.radio.setRecordingActive).toHaveBeenLastCalledWith(true);
    mocks.transport.recordingSessionId = null;
    page.rerender(<Karaoke />);
    expect(mocks.radio.setRecordingActive).toHaveBeenLastCalledWith(false);
  });
  test("reports direct monitoring failure", async () => {
    mocks.startMonitoring.mockRejectedValueOnce(new Error("monitor failed"));
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("monitor"));
    await waitFor(() => expect(page.container.querySelector("[role=alert]").textContent).toContain("monitor failed"));
  });
  test("uses the low-latency room stream while an online room is active", async () => {
    mocks.room.room = { host: true };
    mocks.room.setLocalMonitoring.mockResolvedValueOnce(true);
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
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
    expect(mocks.consoleProps.audio.monitoringEnabled).toBe(true);
  });
  test("wires console effect changes, tempo and stopping monitoring", async () => {
    // Notes/lyrics/auto-hide toggles, seek/skip and the mixer volume commit
    // are wired inside KaraokeConsole's own children (tools.jsx, song-strip.jsx,
    // mixer.jsx) from the preferences/transport/audio objects passed straight
    // through here -- see karaoke-console-components.test.jsx for that
    // coverage. This test only checks the pieces Karaoke itself computes:
    // useKaraokeAudio's effect callbacks and useKaraokeTimeline's tempo change.
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("effect"));
    fireEvent.click(page.getByTestId("effect-commit"));
    fireEvent.click(page.getByTestId("tempo"));
    fireEvent.click(page.getByTestId("monitor-off"));
    await waitFor(() => expect(mocks.stopMonitoring).toHaveBeenCalled());
    calledWith(
      [mocks.preferences.setEffectPreset, ["custom"]],
      [mocks.microphone.updateMicrophoneEffects, [{ echo: 0.6 }]],
      [mocks.preferences.setSpeed, [0.5]],
      [mocks.microphone.setMonitoringEnabled, [false]]
    );
  });
  test("does not confirm a preset locally when persistence fails", async () => {
    // useKaraokeAudio's saveEffects only calls setEffectPreset once
    // updateMicrophoneEffects resolves with a non-null value; a null
    // result (persistence rejected) leaves the preset unconfirmed.
    mocks.microphone.updateMicrophoneEffects.mockResolvedValueOnce(null);
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("preset"));
    await waitFor(() => expect(mocks.microphone.updateMicrophoneEffects).toHaveBeenCalled());
    expect(mocks.preferences.setEffectPreset).not.toHaveBeenCalled();
  });
  test("opens analysis result and handles every completion path", async () => {
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    mocks.transport.analysisRecordingId = "rec";
    page.rerender(<Karaoke />);
    expect(page.getByTestId("analysis-modal")).toBeTruthy();
    fireEvent.click(page.getByTestId("analysis-close"));
    verify([mocks.navigate, "toHaveBeenCalledWith", "/", expect.objectContaining({ replace: true })]);
    mocks.transport.analysisRecordingId = "next";
    page.rerender(<Karaoke />);
    fireEvent.click(page.getByTestId("analysis-done"));
    mocks.transport.analysisRecordingId = "last";
    page.rerender(<Karaoke />);
    fireEvent.click(page.getByTestId("analysis-delete"));
    expect(mocks.navigate).toHaveBeenCalledTimes(3);
  });
  test("pauses, resumes and restores radio after the first intro", async () => {
    vi.useFakeTimers();
    mocks.radio.isPlaying = true;
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("play"));
    await vi.runAllTimersAsync();
    await act(async () => mocks.transportOptions.playback.setPlaying(true));
    mocks.radio.turnOn.mockRejectedValueOnce(new Error("radio unavailable"));
    fireEvent.click(page.getByTestId("play"));
    await act(async () => Promise.resolve());
    calledWith([mocks.transport.togglePlay, [{ forcePlaying: false }]], [mocks.radio.turnOn, [{ remember: false, fadeIn: true }]]);
    await act(async () => mocks.transportOptions.playback.setPlaying(false));
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
    await act(async () => Promise.resolve());
    await vi.runAllTimersAsync();
    verify([mocks.transport.togglePlay, "toHaveBeenCalledWith", { forcePlaying: true }], [events, "toContain", false]);
    page.unmount();
    window.removeEventListener("app:route-blackout", listener);
  });
  test("still completes the auto-start intro when no media element ever mounts", async () => {
    // The readiness guard in useKaraokeSceneFlow only re-schedules while a
    // mounted media element reports readyState < 3; with no element at all
    // it treats "nothing to wait for" as ready and lets the timed intro
    // sequence run to completion instead of hanging forever.
    vi.useFakeTimers();
    mocks.location = { state: { songId: "song", autoPlay: true } };
    mocks.renderMedia = false;
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    await vi.runAllTimersAsync();
    verify([mocks.transport.togglePlay, "toHaveBeenCalledWith", { forcePlaying: true }], [mocks.stageProps.sceneBlackout, "toBe", false]);
    page.unmount();
  });
  test("continues intro after media readiness timeout and handles media-ended callback", async () => {
    vi.useFakeTimers();
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
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
    await act(async () => Promise.resolve());
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
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("play"));
    await vi.runAllTimersAsync();
    await act(async () => mocks.transportOptions.playback.setPlaying(true));
    fireEvent.click(page.getByTestId("play"));
    await act(async () => Promise.resolve());
    fireEvent.click(page.getByTestId("stop"));
    await vi.runAllTimersAsync();
    expect(mocks.transport.stop).toHaveBeenCalled();
  });
  test("uses only lyricsSync tempo and key, ignoring the song's own override fields", async () => {
    mocks.songsPoll = { data: [{ ...song, key_override: "F#", tempo_override: 42 }], error: null };
    mocks.getResult.mockResolvedValueOnce({
      ...result,
      lyrics_sync: { ...result.lyrics_sync, bpm: 137, key: "D" }
    });
    render(<Karaoke />);
    await act(async () => Promise.resolve());
    verify(
      [mocks.consoleProps.timeline.currentTempo, "toBe", 137],
      [mocks.consoleProps.timeline.compactKey, "toContain", "D"]
    );
  });
  test("ignores an auto-start callback retained after unmount", async () => {
    mocks.location = { state: { songId: "song", autoPlay: true } };
    let autoStart;
    const nativeSetTimeout = window.setTimeout;
    vi.spyOn(window, "setTimeout").mockImplementation((callback, timeout, ...args) => {
      if (timeout === 80) autoStart = callback;
      return nativeSetTimeout(callback, timeout, ...args);
    });
    const page = render(<Karaoke />);
    await act(async () => Promise.resolve());
    page.unmount();
    expect(() => autoStart()).not.toThrow();
  });
});
