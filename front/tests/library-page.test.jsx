/* eslint-disable max-len */
/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { same, verify } from "./helpers/assertions.mjs";
import { passthrough } from "./helpers/mocks.mjs";

const mocks = vi.hoisted(() => ({
  location: { state: null },
  navigate: vi.fn(),
  notify: vi.fn(),
  confirm: vi.fn(),
  reloadSettings: vi.fn(),
  settings: { online_name: "Singer" },
  sharedRoom: {
    room: null,
    roomUi: {},
    participants: [],
    syncUi: vi.fn(),
    openKaraoke: vi.fn(),
    requestSongSync: vi.fn()
  },
  polls: [],
  pollIndex: 0,
  refreshes: [],
  importOptions: null,
  actionOptions: null,
  roomOptions: null,
  pollOptions: [],
  deleteRecording: vi.fn(),
  cancelProcessing: vi.fn(),
  resolveSongRevision: vi.fn(),
  setProcessingLoadActive: vi.fn(),
  review: null
}));
vi.mock("react-router-dom", () => ({
  useLocation: () => mocks.location,
  useNavigate: () => mocks.navigate
}));
vi.mock("../src/contexts/AppDialog", () => ({
  useAppDialog: () => ({ alert: mocks.notify, confirm: mocks.confirm })
}));
vi.mock("../src/contexts/OnlineRoomContext", () => ({ useOnlineRoom: () => mocks.sharedRoom }));
vi.mock("../src/hooks/useAppSettings", () => ({
  default: () => ({ reloadSettings: mocks.reloadSettings, settings: mocks.settings })
}));
vi.mock("../src/hooks/usePolling", () => ({
  usePolling: (request, _interval, _deps, options) => {
    request();
    mocks.pollOptions.push(options);
    const value = mocks.polls[mocks.pollIndex++ % mocks.polls.length] || {};
    const refresh = value.refresh || vi.fn();
    mocks.refreshes.push(refresh);
    return { data: null, error: null, ...value, refresh };
  }
}));
vi.mock("../src/utils/performance-profile", () => ({
  setProcessingLoadActive: mocks.setProcessingLoadActive
}));
vi.mock("../src/api/client", () => ({
  api: {
    listSongs: vi.fn(),
    listRecordingsForSong: vi.fn(),
    getStatus: vi.fn(),
    resolveSongRevision: mocks.resolveSongRevision,
    deleteRecording: mocks.deleteRecording,
    cancelProcessing: mocks.cancelProcessing
  }
}));
vi.mock("../src/pages/Library/hooks/use-fileImport", () => ({
  default: (options) => {
    mocks.importOptions = options;
    return {
      importing: false,
      importFile: vi.fn(),
      openFilePicker: vi.fn(),
      review: mocks.review
    };
  }
}));
vi.mock("../src/pages/Library/hooks/use-song-actions", () => ({
  default: (options) => {
    mocks.actionOptions = options;
    return {
      deleteSong: vi.fn(),
      openSongFolder: vi.fn(),
      processSong: vi.fn(),
      reprocessSong: vi.fn()
    };
  }
}));
vi.mock("../src/pages/Library/hooks/use-room-sync", () => ({
  default: (options) => {
    mocks.roomOptions = options;
  }
}));
vi.mock("../src/theme/ui", () => ({
  Box: ({ sx, ...props }) => <div style={sx} {...props} />,
  Stack: passthrough("div"),
  Grid: passthrough("div")
}));
vi.mock("../src/pages/Library/backdrop", () => ({
  QuantumFieldBackdrop: () => <div data-testid="backdrop" />
}));
vi.mock("../src/pages/Library/hero", () => ({
  default: (props) => (
    <div data-testid="hero">
      <span>{props.songCount}</span>
      <button type="button" data-testid="open-room" onClick={props.onOpenRoom} />
    </div>
  )
}));
vi.mock("../src/pages/Library/songs-grid", () => ({
  default: ({ state, processing, recordings }) =>
    state.songsError ? (
      <p role="alert">{state.songsError.message}</p>
    ) : state.filteredSongs.length ? (
      state.filteredSongs.map((song) => (
        <div key={song.id} data-testid={`song-${song.id}`}>
          <button type="button" data-testid="karaoke" onClick={() => state.openKaraoke(song)} />
          <button type="button" data-testid="processing" onClick={() => processing.track(song)} />
          <button type="button" data-testid="recordings" onClick={() => recordings.setSong(song)} />
          <button type="button" data-testid="song-settings" onClick={() => state.setSettingsSongId(song.id)} />
        </div>
      ))
    ) : (
      <p data-testid="empty-library">empty</p>
    )
}));
vi.mock("../src/pages/OnlineRoom", () => ({
  OnlineRoomModal: ({ onClose }) => <button data-testid="room-modal" onClick={onClose} />
}));
vi.mock("../src/pages/Library/modals", () => ({
  AddSongsModal: () => null,
  ProcessingModal: ({ song, onCancel, onClose, onOpenKaraoke }) =>
    song ? (
      <div data-testid="processing-modal">
        <button data-testid="cancel-processing" onClick={onCancel} />
        <button data-testid="close-processing" onClick={onClose} />
        <button data-testid="open-processed" onClick={() => onOpenKaraoke(song.id)} />
      </div>
    ) : null,
  RecordingsModal: ({ song, onAnalyze, onDelete, onClose }) =>
    song ? (
      <div data-testid="recordings-modal">
        <button data-testid="analyze" onClick={() => onAnalyze({ id: "rec" })} />
        <button data-testid="delete-recording" onClick={() => onDelete({ id: "rec" })} />
        <button data-testid="close-recordings" onClick={onClose} />
      </div>
    ) : null
}));
vi.mock("../src/pages/Library/modals/song-settings", () => ({
  default: ({ songId, onClose }) => (
    <button data-testid="song-settings-modal" onClick={onClose}>
      {songId}
    </button>
  )
}));
vi.mock("../src/pages/Karaoke/analysis-modal", () => ({
  default: ({ onClose, onDone, onDeleted }) => (
    <div data-testid="analysis">
      <button data-testid="analysis-close" onClick={onClose} />
      <button data-testid="analysis-done" onClick={onDone} />
      <button data-testid="analysis-deleted" onClick={onDeleted} />
    </div>
  )
}));
import Library from "../src/pages/Library/index.jsx";
const songs = [
  { id: "one", title: "One", artist: "Artist", status: "done" },
  { id: "two", title: "Two", status: "pending" }
];
beforeEach(() => {
  mocks.location = { state: null };
  mocks.navigate.mockReset();
  mocks.notify.mockReset().mockResolvedValue(undefined);
  mocks.confirm.mockReset().mockResolvedValue(true);
  mocks.reloadSettings.mockReset().mockResolvedValue({ online_name: "Singer" });
  mocks.settings = { online_name: "Singer" };
  mocks.sharedRoom.room = null;
  mocks.sharedRoom.roomUi = {};
  mocks.sharedRoom.participants = [];
  mocks.sharedRoom.openKaraoke.mockReset().mockResolvedValue(true);
  mocks.sharedRoom.requestSongSync.mockReset().mockResolvedValue(true);
  mocks.resolveSongRevision.mockReset().mockResolvedValue(null);
  mocks.deleteRecording.mockReset().mockResolvedValue(undefined);
  mocks.cancelProcessing.mockReset().mockResolvedValue(undefined);
  mocks.setProcessingLoadActive.mockReset();
  mocks.review = null;
  mocks.pollIndex = 0;
  mocks.pollOptions = [];
  mocks.refreshes = [];
  mocks.polls = [{ data: songs }, { data: [] }, { data: null }];
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
describe("library page", () => {
  test("renders songs and opens room only after validating the online name", async () => {
    const result = render(<Library />);
    expect(result.getByTestId("song-one")).not.toBeNull();
    fireEvent.click(result.getByTestId("open-room"));
    await waitFor(() => expect(result.getByTestId("room-modal")).not.toBeNull());
    fireEvent.click(result.getByTestId("room-modal"));
    mocks.reloadSettings.mockResolvedValueOnce({ online_name: " " });
    fireEvent.click(result.getByTestId("open-room"));
    await waitFor(() => expect(mocks.notify).toHaveBeenCalled());
    mocks.reloadSettings.mockRejectedValueOnce(new Error("settings"));
    fireEvent.click(result.getByTestId("open-room"));
    await waitFor(() => expect(mocks.notify).toHaveBeenCalledTimes(2));
  });
  test("flags the reduced-performance profile while any song is processing or queued", async () => {
    // Vocal separation already saturates the GPU on its own; the always-on
    // background animations were competing with it for the same GPU and
    // freezing the whole system, which only cleared up once the renderer's
    // GPU process was killed by closing the window. Suppress those
    // animations for as long as any song is actively processing/queued.
    mocks.polls = [{ data: [{ id: "one", title: "One", status: "queued" }] }, { data: [] }, { data: null }];
    const result = render(<Library />);
    await waitFor(() => expect(mocks.setProcessingLoadActive).toHaveBeenCalledWith(true));
    expect(result.queryByTestId("backdrop")).toBeNull();
    cleanup();
    mocks.setProcessingLoadActive.mockClear();
    mocks.pollIndex = 0;
    mocks.polls = [{ data: songs }, { data: [] }, { data: null }];
    const idle = render(<Library />);
    await waitFor(() => expect(mocks.setProcessingLoadActive).toHaveBeenCalledWith(false));
    expect(idle.getByTestId("backdrop")).not.toBeNull();
  });
  test("navigates to karaoke after transition and handles room refusal", async () => {
    vi.useFakeTimers();
    const result = render(<Library />);
    const open = result.getAllByTestId("karaoke")[0];
    fireEvent.click(open);
    fireEvent.click(open);
    await vi.advanceTimersByTimeAsync(920);
    verify([mocks.navigate, "toHaveBeenCalledWith", "/karaoke", { state: { songId: "one", autoPlay: true } }]);
    cleanup();
    mocks.sharedRoom.room = { host: false };
    mocks.sharedRoom.openKaraoke.mockResolvedValueOnce(false);
    mocks.pollIndex = 0;
    const room = render(<Library />);
    fireEvent.click(room.getAllByTestId("karaoke")[0]);
    await Promise.resolve();
    expect(mocks.navigate).not.toHaveBeenCalledTimes(2);
    room.unmount();
    mocks.sharedRoom.openKaraoke.mockResolvedValueOnce(false);
    mocks.pollIndex = 0;
    const accepted = render(<Library />);
    fireEvent.click(accepted.getAllByTestId("karaoke")[0]);
    await Promise.resolve();
    expect(mocks.navigate).toHaveBeenCalledTimes(1);
    expect(mocks.sharedRoom.openKaraoke).toHaveBeenLastCalledWith("one", {
      ownerId: undefined,
      revision: undefined
    });
  });
  test("host opens the local id returned after downloading a participant song", async () => {
    vi.useFakeTimers();
    mocks.sharedRoom.room = { host: true, selfId: "host" };
    mocks.sharedRoom.roomUi = {
      songsByParticipant: {
        guest: [
          {
            id: "guest-song",
            title: "Shared",
            status: "done",
            __roomOwnerId: "guest",
            __roomRevision: "sha256:shared"
          }
        ]
      }
    };
    mocks.sharedRoom.requestSongSync.mockResolvedValueOnce("host-local-song");
    const result = render(<Library />);

    fireEvent.click(result.getAllByTestId("karaoke")[2]);
    await act(async () => Promise.resolve());
    expect(mocks.sharedRoom.openKaraoke).toHaveBeenCalledWith("host-local-song");
    await vi.advanceTimersByTimeAsync(920);
    expect(mocks.navigate).toHaveBeenCalledWith("/karaoke", {
      state: { songId: "host-local-song", autoPlay: true }
    });
  });
  test("tracks processing, cancels work and opens completed song", async () => {
    const result = render(<Library />);
    fireEvent.click(result.getAllByTestId("processing")[0]);
    expect(result.getByTestId("processing-modal")).not.toBeNull();
    fireEvent.click(result.getByTestId("cancel-processing"));
    await waitFor(() => expect(mocks.cancelProcessing).toHaveBeenCalledWith("one"));
    vi.useFakeTimers();
    fireEvent.click(result.getByTestId("open-processed"));
    expect(mocks.navigate).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(920);
    expect(mocks.navigate).toHaveBeenCalledWith("/karaoke", {
      state: { songId: "one", autoPlay: true }
    });
    fireEvent.click(result.getByTestId("close-processing"));
    expect(result.queryByTestId("processing-modal")).toBeNull();
  });
  test("does not stack processing over the import review", () => {
    mocks.review = { title: "New song" };
    mocks.polls = [{ data: [{ ...songs[1], status: "processing" }] }, { data: [] }, { data: null }];
    const result = render(<Library />);
    expect(result.queryByTestId("processing-modal")).toBeNull();
  });
  test("closes an active processing modal without immediately reopening it", async () => {
    const processingSong = { ...songs[1], status: "processing" };
    mocks.polls = [{ data: [songs[0], processingSong] }, { data: [] }, { data: null }];
    const result = render(<Library />);
    await waitFor(() => expect(result.getByTestId("processing-modal")).not.toBeNull());
    fireEvent.click(result.getByTestId("close-processing"));
    expect(result.queryByTestId("processing-modal")).toBeNull();
  });
  test("opens recordings, deletes with confirmation and enters analysis", async () => {
    const result = render(<Library />);
    fireEvent.click(result.getAllByTestId("recordings")[0]);
    fireEvent.click(result.getByTestId("delete-recording"));
    await waitFor(() => expect(mocks.deleteRecording).toHaveBeenCalledWith("rec"));
    fireEvent.click(result.getByTestId("analyze"));
    expect(result.getByTestId("analysis")).not.toBeNull();
    fireEvent.click(result.getByTestId("analysis-close"));
    expect(result.queryByTestId("analysis")).toBeNull();
  });
  test("shows list errors, empty state and releases return blackout", async () => {
    vi.useFakeTimers();
    mocks.location = { state: { fromKaraokeFade: true, analysisRecordingId: "rec" } };
    mocks.polls = [{ data: [], error: new Error("offline") }, { data: [] }, { data: null }];
    mocks.pollIndex = 0;
    const result = render(<Library />);
    verify([result.getByRole("alert").textContent, "toContain", "offline"]);
    expect(result.getByTestId("analysis")).not.toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(120));
    verify([result.container.querySelector('[data-role="library-transition-blackout"]').style.opacity, "toBe", "0"]);
    cleanup();
    vi.useRealTimers();
    mocks.location = { state: null };
    mocks.polls = [{ data: [] }, { data: [] }, { data: null }];
    mocks.pollIndex = 0;
    const empty = render(<Library />);
    expect(empty.getByTestId("empty-library")).not.toBeNull();
  });
  test("connects import and action hooks to page state", async () => {
    const result = render(<Library />);
    act(() => mocks.importOptions.onStarted(songs[1]));
    verify([mocks.actionOptions.processingSongId, "toBe", "two"], [mocks.roomOptions.localSongs, "toHaveLength", 2]);
    fireEvent.click(result.getAllByTestId("song-settings")[0]);
    await waitFor(() => expect(result.getByTestId("song-settings-modal").textContent).toBe("one"));
    fireEvent.click(result.getByTestId("song-settings-modal"));
    expect(result.queryByTestId("song-settings-modal")).toBeNull();
    const processingOptions = mocks.pollOptions.find((options) => options?.shouldContinue);
    verify([processingOptions.shouldContinue({ status: "processing" }), "toBe", true]);
    same(
      [processingOptions.shouldContinue({ status: "done" }), false],
      [processingOptions.shouldRetryError({ status: 500 }), true],
      [processingOptions.shouldRetryError({ status: 404 }), false]
    );
  });
  test("handles declined and failed destructive actions", async () => {
    const result = render(<Library />);
    fireEvent.click(result.getAllByTestId("recordings")[0]);
    mocks.confirm.mockResolvedValueOnce(false);
    fireEvent.click(result.getByTestId("delete-recording"));
    await act(async () => Promise.resolve());
    expect(mocks.deleteRecording).not.toHaveBeenCalled();
    mocks.confirm.mockResolvedValueOnce(true);
    mocks.deleteRecording.mockRejectedValueOnce(new Error("delete failed"));
    fireEvent.click(result.getByTestId("delete-recording"));
    await waitFor(() => expect(mocks.notify).toHaveBeenCalled());
    fireEvent.click(result.getByTestId("close-recordings"));
    fireEvent.click(result.getAllByTestId("processing")[0]);
    mocks.confirm.mockResolvedValueOnce(false);
    fireEvent.click(result.getByTestId("cancel-processing"));
    await act(async () => Promise.resolve());
    expect(mocks.cancelProcessing).not.toHaveBeenCalled();
    mocks.confirm.mockResolvedValueOnce(true);
    mocks.cancelProcessing.mockRejectedValueOnce(new Error("cancel failed"));
    fireEvent.click(result.getByTestId("cancel-processing"));
    await waitFor(() => expect(mocks.notify).toHaveBeenCalledTimes(2));
  });
  test("recovers a rejected room open and clears all analysis callbacks", async () => {
    mocks.sharedRoom.room = { host: true };
    mocks.sharedRoom.openKaraoke.mockRejectedValueOnce(new Error("room failed"));
    const result = render(<Library />);
    fireEvent.click(result.getAllByTestId("karaoke")[0]);
    await waitFor(() => expect(mocks.notify).toHaveBeenCalled());
    verify([result.container.querySelector('[data-role="library-transition-blackout"]').style.opacity, "toBe", "0"]);
    fireEvent.click(result.getAllByTestId("recordings")[0]);
    fireEvent.click(result.getByTestId("analyze"));
    fireEvent.click(result.getByTestId("analysis-done"));
    fireEvent.click(result.getAllByTestId("recordings")[0]);
    fireEvent.click(result.getByTestId("analyze"));
    fireEvent.click(result.getByTestId("analysis-deleted"));
    expect(result.queryByTestId("analysis")).toBeNull();
  });
  test("tracks terminal processing status and clears missing backend jobs", async () => {
    const processingSong = { ...songs[1], status: "processing" };
    const refresh = vi.fn().mockResolvedValue(undefined);
    mocks.polls = [
      { data: [songs[0], processingSong], refresh },
      { data: [] },
      {
        data: {
          song_id: "two",
          status: "done",
          progress_step: "complete",
          progress_percent: 100,
          error_message: null
        }
      }
    ];
    const result = render(<Library />);
    act(() => mocks.importOptions.onStarted(processingSong));
    await waitFor(() => expect(refresh).toHaveBeenCalled());
    expect(result.getByTestId("processing-modal")).toBeTruthy();
    cleanup();
    mocks.pollIndex = 0;
    mocks.polls = [{ data: [songs[0], processingSong] }, { data: [] }, { data: null, error: { status: 404 } }];
    render(<Library />);
    await act(async () => Promise.resolve());
  });
  test("covers missing processing ids, recordings and stale refresh completion", async () => {
    let resolveRefresh;
    const refresh = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveRefresh = resolve;
        })
    );
    mocks.settings = null;
    mocks.polls = [{ data: [{ title: "Working", status: "processing" }], refresh }, { data: null }, { data: null }];
    const result = render(<Library />);
    act(() => mocks.importOptions.onStarted(null));
    fireEvent.click(result.getByTestId("open-room"));
    await waitFor(() => expect(result.getByTestId("room-modal")).not.toBeNull());
    result.unmount();
    if (resolveRefresh) await act(async () => resolveRefresh());
    let resolveTerminalRefresh;
    const terminalRefreshPromise = new Promise((resolve) => {
      resolveTerminalRefresh = resolve;
    });
    const terminalRefresh = vi.fn(() => terminalRefreshPromise);
    mocks.pollIndex = 0;
    mocks.polls = [
      { data: [{ ...songs[1], status: "processing" }], refresh: terminalRefresh },
      { data: null },
      { data: { song_id: "two", status: "done", progress_percent: 100 } }
    ];
    const terminal = render(<Library />);
    act(() => mocks.importOptions.onStarted({ ...songs[1], status: "processing" }));
    await waitFor(() => expect(terminalRefresh).toHaveBeenCalledTimes(2));
    terminal.unmount();
    await act(async () => resolveTerminalRefresh());
  });
});
