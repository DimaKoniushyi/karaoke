/* @vitest-environment jsdom */
import { fireEvent, render } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import LibrarySongCard from "../src/pages/Library/songs-grid/card/index.jsx";
import { verify } from "./helpers/assertions.mjs";
import { mockUseI18nWithFallback } from "./helpers/mocks.mjs";

vi.mock("../src/i18n", async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useI18n: mockUseI18nWithFallback };
});

const handlers = () => ({
  onDelete: vi.fn(),
  onOpenFolder: vi.fn(),
  onOpenKaraoke: vi.fn(),
  onOpenProcessing: vi.fn(),
  onOpenRecordings: vi.fn(),
  onOpenSettings: vi.fn(),
  onProcess: vi.fn(),
  onReprocess: vi.fn()
});
test("ready song opens only from its explicit play action", () => {
  const actions = handlers();
  const song = {
    id: "song",
    title: "Title",
    artist: "Artist",
    status: "done",
    key_override: "C",
    tempo_override: 120,
    difficulty_override: "easy"
  };
  const view = render(<LibrarySongCard cardIndex={1} song={song} canManageLibrary {...actions} />);
  expect(view.queryByRole("button", { name: /Открыть Title|Відкрити Title/ })).toBeNull();
  expect(view.container.querySelector(".library-song-card").hasAttribute("data-interactive")).toBe(false);
  fireEvent.click(view.getByRole("button", { name: /Воспроизвести|Відтворити/ }));
  expect(actions.onOpenKaraoke).toHaveBeenCalledOnce();
  const nested = view.getByRole("button", { name: /Прослушать записи|Прослухати записи/ });
  fireEvent.click(nested);
  expect(document.querySelector(".ui-popover").hasAttribute("data-open")).toBe(false);
  const menuButton = view.getByRole("button", { name: /Другие действия|Інші дії/ });
  fireEvent.click(menuButton);
  expect(document.querySelector(".ui-popover").hasAttribute("data-open")).toBe(true);
  fireEvent.pointerDown(menuButton);
  fireEvent.click(menuButton);
  expect(document.querySelector(".ui-popover").hasAttribute("data-open")).toBe(false);
  fireEvent.click(menuButton);
  expect(document.querySelector(".ui-popover").hasAttribute("data-open")).toBe(true);
  const settings = view.getByRole("button", { name: /Настройки песни|Налаштування пісні/ });
  fireEvent.click(settings);
  expect(actions.onOpenSettings).toHaveBeenCalledWith("song");
  expect(document.querySelector(".ui-popover").hasAttribute("data-open")).toBe(false);
  verify([actions.onOpenKaraoke, "toHaveBeenCalledOnce"], [view.container.textContent, "toContain", "120 BPM"]);
});
test("song status and clickability never change the library card surface", () => {
  const ready = render(<LibrarySongCard song={{ id: "ready", title: "Ready", status: "done" }} {...handlers()} />);
  const failed = render(<LibrarySongCard song={{ id: "failed", title: "Failed", status: "error" }} {...handlers()} />);
  const readyCard = ready.container.querySelector(".ui-card");
  const failedCard = failed.container.querySelector(".ui-card");

  expect(readyCard.classList.contains("library-song-card")).toBe(true);
  expect(failedCard.classList.contains("library-song-card")).toBe(true);
  expect(readyCard.getAttribute("data-variant")).toBeNull();
  expect(failedCard.getAttribute("data-variant")).toBeNull();
  expect(readyCard.style.background).toBe(failedCard.style.background);
  expect(readyCard.style.background).not.toBe("");
  expect(ready.queryByRole("button", { name: /Открыть Ready|Відкрити Ready/ })).toBeNull();
  expect(readyCard.hasAttribute("data-interactive")).toBe(false);
});
test("working song shows progress and opens its processing modal", () => {
  const actions = handlers();
  const song = { id: "song", title: "Title", status: "processing", progress_percent: 42 };
  const view = render(<LibrarySongCard cardIndex={0} song={song} canManageLibrary {...actions} />);
  expect(view.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("42");
  fireEvent.click(view.getByRole("progressbar").closest("button"));
  expect(actions.onOpenProcessing).toHaveBeenCalledWith(song);
  expect(view.queryByRole("button", { name: /Открыть Title|Відкрити Title/ })).toBeNull();
});
test("an interrupted room transfer offers a clear retry instead of a stuck progress bar", () => {
  // A peer-to-peer song transfer has no resume support -- once it's
  // cancelled or errored, showing a normal progress bar would look like it's
  // still running. It must instead say so plainly and let a click retry.
  const actions = handlers();
  const song = { id: "song", title: "Title", status: "done" };
  const cancelled = render(<LibrarySongCard cardIndex={0} song={song} transferStatus={{ stage: "cancelled", percent: 40 }} {...actions} />);
  expect(cancelled.queryByRole("progressbar")).toBeNull();
  const retryButton = cancelled.getByText(/Передача прервана/).closest("button");
  fireEvent.click(retryButton);
  expect(actions.onOpenKaraoke).toHaveBeenCalledWith(song);
  cancelled.unmount();

  const errored = render(<LibrarySongCard cardIndex={0} song={song} transferStatus={{ stage: "error", percent: 10 }} {...handlers()} />);
  expect(errored.queryByRole("progressbar")).toBeNull();
  expect(errored.getByText(/Передача прервана/)).toBeTruthy();
});
test("an in-progress room transfer still shows the normal progress bar", () => {
  const actions = handlers();
  const song = { id: "song", title: "Title", status: "done" };
  const view = render(<LibrarySongCard cardIndex={0} song={song} transferStatus={{ stage: "receiving", percent: 55 }} {...actions} />);
  expect(view.getByRole("progressbar").getAttribute("aria-valuenow")).toBe("55");
  fireEvent.click(view.getByRole("progressbar").closest("button"));
  expect(actions.onOpenKaraoke).not.toHaveBeenCalled();
});
test("room transfer explains waiting to owners and downloading to participants without the song", () => {
  const status = { stage: "waiting", percent: 25 };
  const owner = render(
    <LibrarySongCard song={{ id: "owned", title: "Owned", status: "done", __roomLocal: true }} transferStatus={status} {...handlers()} />
  );
  expect(owner.getByText(/Ожидаем, пока другие участники получат песню/)).toBeTruthy();
  owner.unmount();
  const receiver = render(
    <LibrarySongCard song={{ id: "remote", title: "Remote", status: "done" }} transferStatus={status} {...handlers()} />
  );
  expect(receiver.getByText(/Песня скачивается/)).toBeTruthy();
});
test("unknown song status uses safe badge fallback", () => {
  const actions = handlers();
  const { container } = render(<LibrarySongCard cardIndex={0} song={{ id: "song", title: "Title", status: "custom" }} {...actions} />);
  expect(container.textContent).toContain("custom");
  const empty = render(<LibrarySongCard cardIndex={0} song={{ id: "empty", title: "Title", status: "" }} {...actions} />);
  expect(empty.container.textContent).toMatch(/Ожидает обработки|Очікує обробки/);
});
