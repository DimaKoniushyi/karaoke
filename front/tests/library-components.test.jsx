/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, expect, test, vi } from "vitest";
import SongCoverArt from "../src/components/SongCoverArt.jsx";
import LibraryActions from "../src/pages/Library/hero/actions.jsx";
import LibraryHero from "../src/pages/Library/hero/index.jsx";
import ProcessingModal from "../src/pages/Library/modals/processing/index.jsx";
import getProcessingFailureInfo from "../src/pages/Library/modals/processing/processing-failure-info.jsx";
import RecordingsModal from "../src/pages/Library/modals/recordings.jsx";
import LibrarySongsGrid, { selectSongTransferStatus } from "../src/pages/Library/songs-grid/index.jsx";
import { getProcessingSongs } from "../src/pages/Library/utils.js";
import ProcessingSignal from "../src/theme/features/ProcessingSignal";
import { called, same, verify } from "./helpers/assertions.mjs";

const mocks = vi.hoisted(() => ({ isPlaying: false, theme: "dark", noSettings: false }));
vi.mock("../src/hooks/useAppSettings", () => ({
  default: () => (mocks.noSettings ? null : { settings: { theme: mocks.theme } })
}));
vi.mock("../src/theme/ui", async (importOriginal) => ({
  ...(await importOriginal()),
  Modal: ({ children, titleProps }) => {
    const Icon = titleProps?.icon;
    return (
      <section>
        {titleProps?.image ? (
          <img className="modal-title__image" src={titleProps.image} alt="" onError={titleProps.onImageError} />
        ) : (
          Icon && <Icon />
        )}
        <h1>{titleProps?.title}</h1>
        <div className="modal-title__actions">{titleProps?.actions}</div>
        {children}
      </section>
    );
  }
}));
vi.mock("../src/components/AudioPlayer", () => ({
  AudioPlayer: ({ src }) => <audio data-testid="player" src={src} />
}));
vi.mock("../src/api/client", () => ({
  api: {
    getPerformanceFileUrl: (id) => `recording/${id}`,
    getSongCoverUrl: (id) => `cover/${id}`,
    getAudioTrackUrl: (id, track) => `song/${id}/${track}`
  }
}));
afterEach(() => {
  cleanup();
  mocks.noSettings = false;
});
test("library actions cover search, room, adding and file selection", async () => {
  const setQuery = vi.fn();
  const onRoom = vi.fn();
  const onAdd = vi.fn();
  const onFile = vi.fn();
  const view = render(
    <LibraryActions
      canManageLibrary
      fileInputRef={createRef()}
      onFileChosen={onFile}
      onAdd={onAdd}
      onOpenRoom={onRoom}
      roomActive={false}
      query="q"
      setQuery={setQuery}
    />
  );
  fireEvent.change(view.getByRole("textbox", { name: "Поиск" }), { target: { value: "song" } });
  fireEvent.click(view.getByRole("button", { name: /Пить вместе|Співати разом/ }));
  fireEvent.click(view.getByRole("button", { name: /Добавить песню|Додати пісню/ }));
  fireEvent.change(view.container.querySelector('input[type=file][accept*=".mp3"]'), {
    target: { files: [new File(["audio"], "song.mp3", { type: "audio/mpeg" })] }
  });
  expect(setQuery).toHaveBeenCalledWith("song", expect.any(Object));
  called(onRoom, onAdd);
  expect(view.container.querySelector("input[type=file]").accept).toContain(".kar");
  await waitFor(() => called(onFile));
  view.rerender(<LibraryActions canManageLibrary importing onAdd={onAdd} onOpenRoom={onRoom} query="" setQuery={setQuery} />);
});
test("hero and cover reflect saved theme and song counts", () => {
  mocks.theme = "unknown";
  mocks.isPlaying = true;
  const { container, getByText } = render(
    <>
      <LibraryHero songCount={3} readyCount={2} />
      <SongCoverArt song={{ id: "song" }} />
    </>
  );
  verify(
    [getByText("3"), "not.toBeNull"],
    [getByText("2"), "not.toBeNull"],
    [getByText(/всего песен|всього пісень/i), "not.toBeNull"],
    [getByText(/готово к караоке|готове до караоке/i), "not.toBeNull"]
  );
  expect(container.querySelector('img[src="cover/song"]')).not.toBeNull();
  fireEvent.error(container.querySelector('img[src="cover/song"]'));
  expect(container.querySelector('img[src="cover/song"]')).toBeNull();
  expect(container.querySelector(".lucide-music2")).not.toBeNull();
  expect(container.querySelectorAll(".library-song-cover__bar")).toHaveLength(16);
  expect(container.querySelector(".library-song-cover__bar").style.animation).toContain("library-card-wave");
  mocks.noSettings = true;
  verify([() => render(<LibraryHero songCount={0} readyCount={0} />), "not.toThrow"]);
});
test("large song collections render every library card", () => {
  vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  const songs = Array.from({ length: 60 }, (_, index) => ({
    id: `song-${index}`,
    title: `Song ${index}`,
    status: "done"
  }));
  const state = {
    songsError: null,
    canManageLibrary: true,
    filteredSongs: songs,
    transferStatuses: new Map(),
    openKaraoke: vi.fn(),
    setSettingsSongId: vi.fn(),
    songActions: {
      deleteSong: vi.fn(),
      openSongFolder: vi.fn(),
      processSong: vi.fn(),
      reprocessSong: vi.fn()
    }
  };
  const view = render(
    <LibrarySongsGrid
      state={state}
      fileImport={{ importFile: vi.fn(), importing: false }}
      processing={{ track: vi.fn() }}
      recordings={{ setSong: vi.fn() }}
    />
  );
  expect(view.container.querySelectorAll(".library-song-card")).toHaveLength(songs.length);
  expect(view.getAllByRole("button", { name: /Воспроизвести|Відтворити/ })).toHaveLength(songs.length);
  expect(view.queryByRole("button", { name: /Song \d+/ })).toBeNull();
});
test("song card progress prefers the slowest real participant over the room placeholder", () => {
  const statuses = [
    { participantId: "room", songId: "song", stage: "waiting", percent: 0 },
    { participantId: "guest-a", songId: "song", stage: "sending", percent: 47 },
    { participantId: "guest-b", songId: "song", stage: "sending", percent: 22 },
    { participantId: "guest-c", songId: "other", stage: "sending", percent: 5 }
  ];
  expect(selectSongTransferStatus(statuses, "song")).toEqual(statuses[2]);
  expect(selectSongTransferStatus([...statuses, { songId: "song", stage: "error", percent: 0 }], "song").stage).toBe("error");
});
test("processing signal clamps progress and exposes an accessible value", () => {
  const { getByRole, rerender } = render(<ProcessingSignal progress={140} />);
  expect(getByRole("progressbar").getAttribute("aria-valuenow")).toBe("100");
  rerender(<ProcessingSignal progress="bad" compact />);
  expect(getByRole("progressbar").getAttribute("aria-valuenow")).toBe("0");
  rerender(<ProcessingSignal progress={15} url="audio-not-ready-yet" />);
  expect(getByRole("progressbar").querySelector(".ui-waveform__fallback").style.display).toBe("block");
});
test("processing songs keep the active job before the stable queue", () => {
  const queuedA = { id: "a", status: "queued" };
  const done = { id: "done", status: "done" };
  const active = { id: "active", status: "processing" };
  const queuedB = { id: "b", status: "queued" };
  expect(getProcessingSongs([queuedA, done, active, queuedB])).toEqual([active, queuedA, queuedB]);
  const cancelling = { id: "cancel", status: "cancelling" };
  expect(getProcessingSongs([queuedA, cancelling, active])).toEqual([active, cancelling, queuedA]);
  expect(getProcessingSongs(null)).toEqual([]);
});
test("processing failure details classify backend causes without hiding the original reason", () => {
  expect(getProcessingFailureInfo("EngineUnavailableError: CTC model unavailable")).toMatchObject({
    type: "EngineUnavailableError",
    reason: "CTC model unavailable"
  });
  expect(getProcessingFailureInfo("InvalidArtifactError: invalid interval").hint).toContain("интервалы");
  expect(getProcessingFailureInfo("disk failure")).toMatchObject({
    type: "ProcessingError",
    reason: "disk failure"
  });
  expect(getProcessingFailureInfo("").reason).toContain("Причина");
});
test("processing modal covers active, complete, error and absent songs", () => {
  const cancel = vi.fn();
  const close = vi.fn();
  const open = vi.fn();
  const active = render(
    <ProcessingModal
      song={{ id: "song", title: "Song", status: "processing" }}
      status={{ status: "processing", progress_percent: 20, eta_seconds: 5 }}
      onCancel={cancel}
      onClose={close}
      onOpenKaraoke={open}
    />
  );
  fireEvent.click(active.container.querySelector("button"));
  expect(cancel).toHaveBeenCalled();
  const cover = active.container.querySelector(".modal-title__image");
  expect(cover.src).toContain("cover/song");
  fireEvent.error(cover);
  verify(
    [active.container.querySelector(".modal-title__image"), "toBeNull"],
    [active.container.querySelector(".lucide-circle-dot"), "not.toBeNull"]
  );
  active.rerender(
    <ProcessingModal
      song={{ id: "song", title: "Song", status: "done" }}
      status={{ status: "done", error_message: "problem" }}
      onCancel={cancel}
      onClose={close}
      onOpenKaraoke={open}
    />
  );
  expect(active.container.querySelector(".modal-title__image").src).toContain("cover/song");
  const buttons = active.container.querySelectorAll("button");
  fireEvent.click(buttons[0]);
  fireEvent.click(buttons[1]);
  verify([close, "toHaveBeenCalled"], [open, "toHaveBeenCalledWith", "song"]);
  active.rerender(<ProcessingModal song={null} />);
  expect(active.container.firstChild).toBeNull();
  active.rerender(
    <ProcessingModal
      song={{
        id: "song",
        status: "error",
        progress_percent: 77,
        progress_step: "Синхронизация текста",
        error_message: "InvalidArtifactError: Full-song aligner returned invalid timestamps (word 0 has an invalid interval)"
      }}
      onCancel={cancel}
      onClose={close}
      onOpenKaraoke={open}
    />
  );
  const openApplicationLog = vi.fn();
  globalThis.electronAPI = { openApplicationLog };
  expect(active.queryByRole("progressbar")).toBeNull();
  expect(active.getByRole("alert").textContent).toContain("Full-song aligner");
  expect(active.getByRole("alert").textContent).toContain("InvalidArtifactError");
  expect(active.getByRole("alert").textContent).toContain("Синхронизация текста");
  expect(active.getByRole("alert").textContent).toContain("77%");
  fireEvent.click(active.getByRole("button", { name: "Открыть журнал выполнения" }));
  expect(openApplicationLog).toHaveBeenCalledOnce();
  delete globalThis.electronAPI;
});
test("symbolic processing waits for downloaded audio before requesting its waveform", () => {
  const props = {
    onCancel: vi.fn(),
    onClose: vi.fn(),
    onOpenKaraoke: vi.fn()
  };
  const view = render(
    <ProcessingModal
      {...props}
      song={{ id: "kar-song", title: "KAR", status: "processing", original_filename: "song.kar" }}
      status={{ status: "processing", progress_percent: 8 }}
    />
  );
  expect(view.container.querySelector(".ui-waveform__host").style.display).toBe("none");
  view.rerender(
    <ProcessingModal
      {...props}
      song={{ id: "kar-song", title: "KAR", status: "processing", original_filename: "song.kar" }}
      status={{ status: "processing", progress_percent: 34 }}
    />
  );
  expect(view.container.querySelector(".ui-waveform__host").style.display).toBe("block");
});
test("processing modal carousel changes only the viewed queued song", () => {
  const select = vi.fn();
  const songs = [
    { id: "active", title: "Active", artist: "Artist A", status: "processing" },
    { id: "queued", title: "Queued", artist: "Artist B", status: "queued" }
  ];
  const view = render(
    <ProcessingModal
      song={songs[0]}
      songs={songs}
      status={{ song_id: "queued", status: "queued", progress_percent: 77 }}
      onSelectSong={select}
      onCancel={vi.fn()}
      onClose={vi.fn()}
      onOpenKaraoke={vi.fn()}
    />
  );
  expect(view.getByRole("heading").textContent).toBe("Active");
  expect(view.container.textContent).toMatch(/Обрабатывается|Обробляється/);
  const previous = view.getByRole("button", { name: /Предыдущая песня|Попередня пісня/ });
  const next = view.getByRole("button", { name: /Следующая песня|Наступна пісня/ });
  expect(previous.disabled).toBe(true);
  fireEvent.click(next);
  expect(select).toHaveBeenCalledWith(songs[1]);
});
test("recordings modal renders empty, error and recording actions", () => {
  const analyze = vi.fn();
  const remove = vi.fn();
  const result = render(<RecordingsModal song={{ title: "Song" }} recordings={[]} />);
  expect(result.container.textContent).toMatch(/пока нет записанных|ще немає записаних|немає записаних/);
  result.rerender(<RecordingsModal song={{ title: "Song" }} error={new Error("offline")} />);
  verify([result.getByRole("alert").textContent, "toContain", "offline"]);
  result.rerender(
    <RecordingsModal
      song={{ title: "Song" }}
      recordings={[{ id: "rec", created_at: "2026-01-01", duration_sec: 3 }]}
      onAnalyze={analyze}
      onDelete={remove}
    />
  );
  const buttons = result.container.querySelectorAll("button");
  fireEvent.click(buttons[0]);
  fireEvent.click(buttons[1]);
  same([analyze.mock.calls[0][0].id, "rec"], [remove.mock.calls[0][0].id, "rec"]);
  verify([result.getByTestId("player").getAttribute("src"), "toBe", "recording/rec"]);
  result.rerender(<RecordingsModal song={null} />);
  expect(result.container.firstChild).toBeNull();
});
