/* @vitest-environment jsdom */
/* eslint-disable max-len */
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { translateSaved } from "../src/i18n/runtime.js";
import useLibraryFileImport from "../src/pages/Library/hooks/use-fileImport.js";
import useLibraryRoomSync, { capParticipantSongs } from "../src/pages/Library/hooks/use-room-sync.js";
import useLibrarySongActions, { releaseSongMedia } from "../src/pages/Library/hooks/use-song-actions.js";
import { resolveVisibleSongs } from "../src/pages/Library/utils.js";

const api = vi.hoisted(() => ({
  addSong: vi.fn(),
  inspectSongIdentity: vi.fn(),
  processSong: vi.fn(),
  reprocessMelody: vi.fn(),
  deleteSong: vi.fn(),
  getSongRevisions: vi.fn()
}));
const exclusive = vi.hoisted(() => ({ pending: false, run: vi.fn() }));
vi.mock("../src/api/client", () => ({ api }));
vi.mock("../src/hooks/useExclusiveAsyncAction", () => ({ default: () => exclusive }));

beforeEach(() => {
  Object.values(api).forEach((mock) => mock.mockReset());
  api.inspectSongIdentity.mockResolvedValue({});
  exclusive.pending = false;
  exclusive.run.mockReset().mockImplementation((action) => action());
  delete window.electronAPI;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

// importFile only stages a reviewable draft now (AddSongsModal confirms
// title/artist before anything is uploaded) -- confirmDraft() is what
// actually fires api.addSong/processSong. Flushing a macrotask after it
// lets every awaited call inside the queued process() finish, since the
// microtask queue (including chained promise .then()s) always drains
// before a setTimeout callback runs.
const confirmAndFlush = async (result) => {
  await act(async () => {
    result.current.confirmDraft();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
};

describe("library file import", () => {
  test("stages and imports the selected file", async () => {
    const notify = vi.fn();
    const onStarted = vi.fn();
    api.addSong.mockResolvedValue({ id: "song", title: "Track" });
    api.processSong.mockResolvedValue({});
    const { result } = renderHook(() => useLibraryFileImport({ notify, onStarted }));
    const input = { files: [new File(["audio"], "track.mp3")], value: "path" };
    await act(() => result.current.importFile({ currentTarget: input }));
    expect(input.value).toBe("");
    expect(result.current.review).not.toBeNull();
    await confirmAndFlush(result);
    expect(api.addSong).toHaveBeenCalledWith(input.files[0], "track", "");
    expect(api.processSong).toHaveBeenCalledWith("song", "auto");
    expect(onStarted).toHaveBeenCalledWith(expect.objectContaining({ id: "song" }));
    expect(notify).not.toHaveBeenCalled();
  });

  test("tracks changing picker state, callbacks and compound extensions", async () => {
    const firstStarted = vi.fn();
    const secondStarted = vi.fn();
    const props = {
      notify: vi.fn(),
      onStarted: firstStarted
    };
    api.addSong.mockResolvedValue({ id: "song" });
    api.processSong.mockResolvedValue({});
    const hook = renderHook((value) => useLibraryFileImport(value), { initialProps: props });
    hook.rerender({ ...props, onStarted: secondStarted });
    const file = new File(["audio"], "archive.tar.mp3");
    await act(() => hook.result.current.importFile({ currentTarget: { files: [file], value: "path" } }));
    await confirmAndFlush(hook.result);
    expect(api.addSong).toHaveBeenCalledWith(file, "archive.tar", "");
    expect(firstStarted).not.toHaveBeenCalled();
    expect(secondStarted).toHaveBeenCalledWith({ id: "song" });
  });

  test("ignores empty selection and reports failures", async () => {
    const notify = vi.fn().mockResolvedValue(undefined);
    const hook = renderHook(() => useLibraryFileImport({ notify, onStarted: vi.fn() }));
    await act(() => hook.result.current.importFile({ currentTarget: { files: [], value: "x" } }));
    expect(exclusive.run).not.toHaveBeenCalled();
    await act(() => hook.result.current.importFile({ currentTarget: { files: undefined, value: "x" } }));
    expect(exclusive.run).not.toHaveBeenCalled();

    api.addSong.mockRejectedValue(new Error("invalid file"));
    await act(() =>
      hook.result.current.importFile({
        currentTarget: { files: [new File(["x"], "broken")], value: "x" }
      })
    );
    await confirmAndFlush(hook.result);
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("invalid file"));
  });

  test("rolls back a created song when processing cannot start", async () => {
    const notify = vi.fn().mockResolvedValue(undefined);
    api.addSong.mockResolvedValue({ id: "rollback" });
    const processingError = new Error("pipeline busy");
    processingError.status = 409;
    api.processSong.mockRejectedValue(processingError);
    api.deleteSong.mockResolvedValue(null);
    const { result } = renderHook(() => useLibraryFileImport({ notify, onStarted: vi.fn() }));
    await act(() =>
      result.current.importFile({
        currentTarget: { files: [new File(["x"], "track.mp3")], value: "x" }
      })
    );
    await confirmAndFlush(result);
    expect(api.deleteSong).toHaveBeenCalledWith("rollback");
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("pipeline busy"));
  });

  test("keeps a created song after an ambiguous process timeout", async () => {
    const notify = vi.fn().mockResolvedValue(undefined);
    const onStarted = vi.fn();
    api.addSong.mockResolvedValue({ id: "uncertain" });
    const timeout = new Error("deadline");
    timeout.name = "TimeoutError";
    api.processSong.mockRejectedValue(timeout);
    const { result } = renderHook(() => useLibraryFileImport({ notify, onStarted }));
    await act(() =>
      result.current.importFile({
        currentTarget: { files: [new File(["x"], "track.mp3")], value: "x" }
      })
    );
    await confirmAndFlush(result);
    expect(api.deleteSong).not.toHaveBeenCalled();
    expect(onStarted).toHaveBeenCalledWith({ id: "uncertain" });
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("deadline"));
  });
});

describe("library room synchronization", () => {
  test("synchronizes filter popover visibility without echoing remote changes", () => {
    const syncUi = vi.fn();
    const setFiltersOpen = vi.fn();
    const base = {
      localSongs: [],
      query: "",
      filters: {},
      filtersOpen: false,
      room: { host: true, selfId: "self" },
      roomEventId: 1,
      roomFiltersOpen: true,
      participantCount: 1,
      setQuery: vi.fn(),
      setFilters: vi.fn(),
      setFiltersOpen,
      syncUi
    };
    const hook = renderHook((value) => useLibraryRoomSync(value), { initialProps: base });

    expect(setFiltersOpen).toHaveBeenCalledWith(true);
    syncUi.mockClear();
    hook.rerender({ ...base, filtersOpen: true });
    expect(syncUi).not.toHaveBeenCalledWith({ libraryFiltersOpen: true });

    hook.rerender({ ...base, filtersOpen: false, roomFiltersOpen: undefined });
    expect(syncUi).toHaveBeenCalledWith({ libraryFiltersOpen: false });
  });

  test("caps Cyrillic room libraries by UTF-8 bytes", () => {
    const songs = Array.from({ length: 500 }, (_, index) => ({
      id: index,
      title: "Очень длинное кириллическое название песни ".repeat(20)
    }));
    const capped = capParticipantSongs(songs);
    const bytes = new TextEncoder().encode(JSON.stringify({ songs: capped })).byteLength;

    expect(bytes).toBeLessThanOrEqual(120 * 1024);
    expect(capped.length).toBeLessThan(songs.length);
  });

  test("broadcasts local query and host song changes", async () => {
    const syncUi = vi.fn();
    const setQuery = vi.fn();
    const props = {
      localSongs: [{ id: 1 }],
      query: "local",
      room: { host: true, selfId: "self" },
      roomEventId: 1,
      roomQuery: undefined,
      participantCount: 1,
      setQuery,
      syncUi
    };
    const hook = renderHook((value) => useLibraryRoomSync(value), { initialProps: props });
    await act(async () => Promise.resolve());
    expect(syncUi).toHaveBeenCalledWith({ query: "local" });
    expect(syncUi).toHaveBeenCalledWith({ songs: [{ id: 1, __roomOwnerId: "self" }] });
    syncUi.mockClear();
    hook.rerender({ ...props, localSongs: [{ id: 2 }], participantCount: 2 });
    await act(async () => Promise.resolve());
    expect(syncUi).toHaveBeenCalledWith({ songs: [{ id: 2, __roomOwnerId: "self" }] });
    expect(setQuery).not.toHaveBeenCalled();
  });

  test("applies remote query once without echoing it", () => {
    const syncUi = vi.fn();
    const setQuery = vi.fn();
    const base = {
      localSongs: [],
      query: "local",
      room: { host: false },
      roomEventId: 1,
      roomQuery: "remote",
      participantCount: 1,
      setQuery,
      syncUi
    };
    const hook = renderHook((value) => useLibraryRoomSync(value), { initialProps: base });
    expect(setQuery).toHaveBeenCalledWith("remote");
    syncUi.mockClear();
    hook.rerender({ ...base, query: "remote" });
    expect(syncUi).not.toHaveBeenCalled();
    setQuery.mockClear();
    hook.rerender({ ...base, query: "remote", roomQuery: "second-remote" });
    expect(setQuery).toHaveBeenCalledWith("second-remote");
    hook.rerender({ ...base, query: "second-remote", roomQuery: "second-remote" });
    syncUi.mockClear();
    hook.rerender({ ...base, query: "next-local", roomQuery: undefined });
    expect(syncUi).toHaveBeenCalledWith({ query: "next-local" });
    syncUi.mockClear();
    hook.rerender({ ...base, query: "offline", room: null, roomQuery: null });
    expect(syncUi).not.toHaveBeenCalled();
  });

  test("publishes done songs' revisions from a single batched request", async () => {
    const syncUi = vi.fn();
    api.getSongRevisions.mockResolvedValue({
      revisions: [
        { song_id: "done", revision: "sha256:done", error: null },
        { song_id: "broken", revision: null, error: "Could not fingerprint song" }
      ]
    });
    const props = {
      localSongs: [
        { id: "done", status: "done" },
        { id: "broken", status: "done" },
        { id: "queued", status: "processing" }
      ],
      query: "",
      room: { host: true, selfId: "self" },
      roomEventId: 1,
      participantCount: 1,
      setQuery: vi.fn(),
      syncUi
    };
    renderHook((value) => useLibraryRoomSync(value), { initialProps: props });
    await act(async () => Promise.resolve());
    // One batched request for both "done" songs, not one call per song.
    expect(api.getSongRevisions).toHaveBeenCalledOnce();
    expect(api.getSongRevisions).toHaveBeenCalledWith(["done", "broken"]);
    expect(syncUi).toHaveBeenCalledWith({
      songs: [
        { id: "done", status: "done", __roomOwnerId: "self", __roomRevision: "sha256:done" },
        { id: "broken", status: "done", __roomOwnerId: "self" },
        { id: "queued", status: "processing", __roomOwnerId: "self" }
      ]
    });
  });
});

const actionProps = (overrides = {}) => ({
  confirmDialog: vi.fn().mockResolvedValue(true),
  notify: vi.fn().mockResolvedValue(undefined),
  onChanged: vi.fn().mockResolvedValue(undefined),
  processingSongId: "song",
  recordingsSongId: "song",
  setHiddenSongIds: vi.fn((update) => update(new Set())),
  setProcessingSong: vi.fn(),
  setRecordingsSong: vi.fn(),
  ...overrides
});

describe("library song actions", () => {
  test("releases only the deleted song media before removing its files", async () => {
    const target = document.createElement("audio");
    const other = document.createElement("audio");
    target.src = "http://127.0.0.1:18000/songs/song-to-delete/audio/instrumental";
    other.src = "http://127.0.0.1:18000/songs/other-song/audio/instrumental";
    target.pause = vi.fn();
    target.load = vi.fn();
    Object.defineProperty(target, "srcObject", { value: {}, writable: true });
    other.pause = vi.fn();
    other.load = vi.fn();
    document.body.append(target, other);

    await releaseSongMedia("song-to-delete");

    expect(target.pause).toHaveBeenCalledOnce();
    expect(target.load).toHaveBeenCalledOnce();
    expect(target.getAttribute("src")).toBeNull();
    expect(target.srcObject).toBeNull();
    expect(other.pause).not.toHaveBeenCalled();
    expect(other.getAttribute("src")).not.toBeNull();
  });

  test("processes pending songs and confirms reprocessing", async () => {
    const props = actionProps();
    api.processSong.mockResolvedValue({});
    api.reprocessMelody.mockResolvedValue({});
    const { result } = renderHook(() => useLibrarySongActions(props));
    const song = { id: "song", title: "Track", status: "pending" };
    await act(() => result.current.processSong(song));
    expect(props.confirmDialog).not.toHaveBeenCalled();
    expect(api.processSong).toHaveBeenCalledWith("song");
    expect(props.setProcessingSong).toHaveBeenCalledWith(song);

    await act(() => result.current.processSong({ ...song, status: "done" }));
    await act(() => result.current.reprocessSong(song));
    expect(props.confirmDialog).toHaveBeenCalledTimes(2);
    expect(props.confirmDialog.mock.calls[0]).toEqual([
      translateSaved("Вы точно хотите обработать заново песню «{0}»? Ранее созданные результаты обработки будут обновлены.", {
        0: "Track"
      }),
      translateSaved("Обработать песню заново?")
    ]);
    expect(props.confirmDialog.mock.calls[1]).toEqual([
      translateSaved("Вы точно хотите обработать заново песню «{0}»? Текущие данные мелодии будут пересозданы.", { 0: "Track" }),
      translateSaved("Обработать песню заново?")
    ]);
    expect(api.reprocessMelody).toHaveBeenCalledWith("song");
    expect(props.onChanged).toHaveBeenCalledTimes(3);
  });

  test("supports cancellation, invalid songs and processing errors", async () => {
    const props = actionProps({ confirmDialog: vi.fn().mockResolvedValue(false) });
    const hook = renderHook(() => useLibrarySongActions(props));
    await act(() => hook.result.current.processSong({ id: "x", status: "done" }));
    await act(() => hook.result.current.reprocessSong({ id: "x" }));
    await act(() => hook.result.current.processSong(null));
    await act(() => hook.result.current.reprocessSong(null));
    expect(api.processSong).not.toHaveBeenCalled();
    expect(api.reprocessMelody).not.toHaveBeenCalled();
    expect(props.confirmDialog).toHaveBeenCalledTimes(2);
    expect(props.confirmDialog).toHaveBeenLastCalledWith(
      translateSaved("Вы точно хотите обработать заново песню «{0}»? Текущие данные мелодии будут пересозданы.", {
        0: translateSaved("Без названия")
      }),
      translateSaved("Обработать песню заново?")
    );

    props.confirmDialog.mockResolvedValue(true);
    api.processSong.mockRejectedValue(new Error("pipeline busy"));
    await act(() => hook.result.current.processSong({ id: "x", status: "pending" }));
    expect(props.notify).toHaveBeenCalledWith(`${translateSaved("Не удалось запустить обработку")}: pipeline busy`);

    props.confirmDialog.mockResolvedValue(false);
    await act(() => hook.result.current.processSong({ id: "untitled", status: "done" }));
    expect(props.confirmDialog).toHaveBeenLastCalledWith(
      translateSaved("Вы точно хотите обработать заново песню «{0}»? Ранее созданные результаты обработки будут обновлены.", {
        0: translateSaved("Без названия")
      }),
      translateSaved("Обработать песню заново?")
    );

    props.confirmDialog.mockResolvedValue(true);
    api.reprocessMelody.mockRejectedValueOnce(new Error("midi busy"));
    await act(() => hook.result.current.reprocessSong({ id: "midi", title: "Midi" }));
    expect(props.notify).toHaveBeenLastCalledWith(`${translateSaved("Не удалось переобработать мелодию")}: midi busy`);
  });

  test("processing is exclusive, retryable and supports no change callback", async () => {
    let resolveProcessing;
    api.processSong.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveProcessing = resolve;
      })
    );
    const props = actionProps({ onChanged: undefined });
    const hook = renderHook(() => useLibrarySongActions(props));
    const song = { id: "same", status: "pending" };
    let first;
    await act(async () => {
      first = hook.result.current.processSong(song);
      await hook.result.current.processSong(song);
    });
    expect(api.processSong).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveProcessing({});
      await first;
    });
    expect(props.notify).not.toHaveBeenCalled();
    api.processSong.mockResolvedValueOnce({});
    await act(() => hook.result.current.processSong(song));
    expect(api.processSong).toHaveBeenCalledTimes(2);
  });

  test("deletes a song and closes matching library modals", async () => {
    let hidden = new Set(["keep"]);
    const setHiddenSongIds = vi.fn((update) => {
      hidden = update(hidden);
    });
    const props = actionProps({ setHiddenSongIds });
    api.deleteSong.mockResolvedValue({});
    const { result } = renderHook(() => useLibrarySongActions(props));
    await act(() => result.current.deleteSong({ id: "song", title: "Track" }));
    expect(api.deleteSong).toHaveBeenCalledWith("song");
    expect(props.setRecordingsSong).toHaveBeenCalledWith(null);
    expect(props.setProcessingSong).toHaveBeenCalledWith(null);
    expect(hidden).toEqual(new Set(["keep", "song"]));
    expect(props.confirmDialog).toHaveBeenCalledWith(
      translateSaved("Удалить «{0}»? Это удалит все файлы песни.", { 0: "Track" }),
      translateSaved("Удалить песню?")
    );

    const unrelated = actionProps({ processingSongId: "other", recordingsSongId: "other" });
    const unrelatedHook = renderHook(() => useLibrarySongActions(unrelated));
    await act(() => unrelatedHook.result.current.deleteSong({ id: "second", title: "Second" }));
    expect(unrelated.setRecordingsSong).not.toHaveBeenCalled();
    expect(unrelated.setProcessingSong).not.toHaveBeenCalled();
  });

  test("does not report successful backend mutations as failed when refresh breaks", async () => {
    let hidden = new Set();
    const props = actionProps({
      onChanged: vi.fn().mockRejectedValue(new Error("refresh failed")),
      setHiddenSongIds: vi.fn((update) => {
        hidden = update(hidden);
      })
    });
    api.deleteSong.mockResolvedValue({});
    api.processSong.mockResolvedValue({});
    const hook = renderHook(() => useLibrarySongActions(props));
    await act(() => hook.result.current.deleteSong({ id: "gone", title: "Gone" }));
    expect(hidden.has("gone")).toBe(true);
    expect(props.notify).toHaveBeenCalledWith(expect.stringContaining("список"));
    props.notify.mockClear();
    await act(() => hook.result.current.processSong({ id: "processing", status: "pending" }));
    expect(props.setProcessingSong).toHaveBeenCalledWith({ id: "processing", status: "pending" });
    expect(props.notify).toHaveBeenCalledWith(expect.stringContaining("список"));
  });

  test("keeps an ambiguously deleted song hidden until reconciliation succeeds", async () => {
    let hidden = new Set();
    const timeout = new Error("timeout");
    timeout.name = "TimeoutError";
    const props = actionProps({
      onChanged: vi.fn().mockRejectedValue(new Error("refresh failed")),
      setHiddenSongIds: vi.fn((update) => {
        hidden = update(hidden);
      })
    });
    api.deleteSong.mockRejectedValueOnce(timeout);
    const hook = renderHook(() => useLibrarySongActions(props));
    await act(() => hook.result.current.deleteSong({ id: "maybe-gone", title: "Track" }));
    expect(hidden.has("maybe-gone")).toBe(true);
    expect(props.onChanged).toHaveBeenCalledOnce();
    expect(props.notify).toHaveBeenCalledWith(expect.stringContaining("проверяем состояние"));
  });

  test("rolls back failed deletion and handles confirmation errors", async () => {
    let hidden = new Set(["keep"]);
    const props = actionProps({
      setHiddenSongIds: vi.fn((update) => {
        hidden = update(hidden);
      })
    });
    api.deleteSong.mockRejectedValueOnce(new Error("locked"));
    const hook = renderHook(() => useLibrarySongActions(props));
    await act(() => hook.result.current.deleteSong({ id: "x", title: "Track" }));
    expect(hidden).toEqual(new Set(["keep"]));
    expect(props.notify).toHaveBeenCalledWith(translateSaved("Не удалось удалить: {0}", { 0: "locked" }));

    props.confirmDialog.mockRejectedValueOnce(new Error("dialog failed"));
    await act(() => hook.result.current.deleteSong({ id: "y", title: "Track" }));
    expect(props.notify).toHaveBeenCalledWith(translateSaved("Не удалось подтвердить удаление: {0}", { 0: "dialog failed" }));
    props.confirmDialog.mockResolvedValueOnce(false).mockResolvedValueOnce(false);
    await act(() => hook.result.current.deleteSong({ id: "z", title: "Track" }));
    await act(() => hook.result.current.deleteSong({ id: "z", title: "Track" }));
    expect(props.confirmDialog).toHaveBeenCalledTimes(4);
    expect(api.deleteSong).toHaveBeenCalledTimes(1);
    await act(() => hook.result.current.deleteSong(null));
  });

  test("actions use the latest callbacks after rerender", async () => {
    const firstNotify = vi.fn();
    const secondNotify = vi.fn().mockResolvedValue(undefined);
    const firstConfirm = vi.fn().mockResolvedValue(false);
    const secondConfirm = vi.fn().mockResolvedValue(true);
    const initial = actionProps({
      confirmDialog: firstConfirm,
      notify: firstNotify,
      onChanged: undefined
    });
    const hook = renderHook((value) => useLibrarySongActions(value), { initialProps: initial });
    const latest = { ...initial, confirmDialog: secondConfirm, notify: secondNotify };
    hook.rerender(latest);
    api.processSong.mockRejectedValueOnce(new Error("latest processing"));
    await act(() => hook.result.current.processSong({ id: "p", status: "pending" }));
    expect(firstNotify).not.toHaveBeenCalled();
    expect(secondNotify).toHaveBeenCalledWith(`${translateSaved("Не удалось запустить обработку")}: latest processing`);

    secondNotify.mockClear();
    api.reprocessMelody.mockRejectedValueOnce(new Error("latest midi"));
    await act(() => hook.result.current.reprocessSong({ id: "m", title: "Midi" }));
    expect(firstNotify).not.toHaveBeenCalled();
    expect(secondNotify).toHaveBeenCalledWith(`${translateSaved("Не удалось переобработать мелодию")}: latest midi`);

    secondNotify.mockClear();
    api.deleteSong.mockResolvedValueOnce({});
    await act(() => hook.result.current.deleteSong({ id: "d", title: "Delete" }));
    expect(firstConfirm).not.toHaveBeenCalled();
    expect(secondConfirm).toHaveBeenCalled();
    expect(secondNotify).not.toHaveBeenCalled();

    delete window.electronAPI;
    await act(() => hook.result.current.openSongFolder({ id: "d" }));
    expect(secondNotify).toHaveBeenCalledWith(translateSaved("Открытие папки доступно только в установленном приложении."));
  });

  test("opens song folders through Electron and reports all fallback errors", async () => {
    const props = actionProps();
    const hook = renderHook(() => useLibrarySongActions(props));
    await act(() => hook.result.current.openSongFolder({ id: "song" }));
    expect(props.notify).toHaveBeenCalledWith(translateSaved("Открытие папки доступно только в установленном приложении."));
    props.notify.mockClear();

    const openSongFolder = vi
      .fn()
      .mockResolvedValueOnce("Windows error")
      .mockResolvedValueOnce("")
      .mockRejectedValueOnce(new Error("ipc failed"));
    window.electronAPI = { openSongFolder };
    const song = { output_dir: "path", slug: "slug", title: "Title", id: "id" };
    await act(() => hook.result.current.openSongFolder(song));
    expect(props.notify).toHaveBeenLastCalledWith("Windows error", translateSaved("Не удалось открыть папку"));
    await act(() => hook.result.current.openSongFolder(song));
    expect(props.notify).toHaveBeenCalledTimes(1);
    await act(() => hook.result.current.openSongFolder(song));
    expect(props.notify).toHaveBeenLastCalledWith(
      translateSaved("Не удалось открыть папку: {0}", { 0: "ipc failed" }),
      translateSaved("Не удалось открыть папку")
    );
    expect(openSongFolder).toHaveBeenCalledWith({
      path: "path",
      slug: "slug",
      title: "Title",
      id: "id"
    });
    await act(() => hook.result.current.openSongFolder({}));
    expect(openSongFolder).toHaveBeenLastCalledWith({ path: "", slug: "", title: "", id: "" });
    expect(props.notify).toHaveBeenCalledTimes(2);
  });
});

describe("resolveVisibleSongs merges the room's shared library", () => {
  const local = [{ id: "mine", title: "Mine" }];

  test("returns only local songs outside a room", () => {
    expect(resolveVisibleSongs({ localSongs: local, room: null })).toEqual(local);
  });

  test("the host sees their own library together with every participant library", () => {
    expect(
      resolveVisibleSongs({
        localSongs: local,
        room: { host: true, selfId: "host" },
        roomSongsByParticipant: { guest: [{ id: "guest-song", title: "Guest song" }] }
      })
    ).toEqual([
      { id: "mine", title: "Mine", __roomLocal: true, __roomOwnerId: "host" },
      { id: "guest-song", title: "Guest song" }
    ]);
  });

  test("a guest sees their own songs and the host's broadcast room songs", () => {
    const roomSongs = [{ id: "host-song", title: "Host song" }];
    expect(resolveVisibleSongs({ localSongs: local, room: { host: false, selfId: "guest" }, roomSongs })).toEqual([
      { id: "mine", title: "Mine", __roomLocal: true, __roomOwnerId: "guest" },
      ...roomSongs
    ]);
  });

  test("a guest sees both per-participant songs and the flat room list combined", () => {
    const roomSongs = [{ id: "host-song", title: "Host song" }];
    const roomSongsByParticipant = { "peer-id": [{ id: "peer-song", title: "Peer song" }] };
    expect(
      resolveVisibleSongs({
        localSongs: local,
        room: { host: false, selfId: "guest" },
        roomSongs,
        roomSongsByParticipant
      })
    ).toEqual([
      { id: "mine", title: "Mine", __roomLocal: true, __roomOwnerId: "guest" },
      { id: "host-song", title: "Host song" },
      { id: "peer-song", title: "Peer song" }
    ]);
  });

  test("falls back to local songs when no remote song list is available", () => {
    expect(
      resolveVisibleSongs({
        localSongs: local,
        room: { host: false, selfId: "guest" },
        roomSongs: [],
        roomSongsByParticipant: {}
      })
    ).toEqual([{ id: "mine", title: "Mine", __roomLocal: true, __roomOwnerId: "guest" }]);
  });

  test("ignores malformed remote entries instead of throwing", () => {
    const roomSongsByParticipant = { "peer-id": [null, "bad", { title: "no id" }, { id: "ok" }] };
    expect(resolveVisibleSongs({ localSongs: [], room: { host: false }, roomSongsByParticipant })).toEqual([{ id: "ok" }]);
  });
});
