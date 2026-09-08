/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import AddSongsModal from "../src/pages/Library/modals/add-song/index.jsx";
import SelectedFilePreview from "../src/pages/Library/modals/add-song/select-file-preview.jsx";

beforeEach(() => {
  URL.createObjectURL = vi.fn((file) => `blob:${file.name}`);
  URL.revokeObjectURL = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

test("song confirmation presents metadata in a compact two-field layout", async () => {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();

  const view = render(
    <AddSongsModal
      review={{
        index: 0,
        items: [
          {
            file: new File(["audio"], "Artist - Track.flac"),
            coverUrl: "data:image/png;base64,cover",
            title: "Track",
            artist: "Artist",
            processingMode: "auto"
          }
        ]
      }}
      onCancel={onCancel}
      onConfirm={onConfirm}
    />
  );

  expect(view.getAllByRole("textbox")).toHaveLength(2);
  expect(view.container.querySelector(".ui-modal-title-media img")?.src).toContain("data:image/png;base64,cover");
  expect(view.container.querySelectorAll(".ui-text-field-outline legend")).toHaveLength(3);
  expect(view.getByText(/Авто ·/)).not.toBeNull();
  const title = view.getByDisplayValue("Track");
  const artist = view.getByDisplayValue("Artist");
  const preview = view.getByRole("button", { name: /Прослушать|Прослухати/ });
  const audio = view.container.querySelector("audio");
  expect(preview.disabled).toBe(false);
  expect(preview.getAttribute("aria-label")).toMatch(/Прослушать|Прослухати/);
  expect(audio.src).toContain("blob:Artist - Track.flac");
  audio.play = vi.fn().mockResolvedValue(undefined);
  audio.pause = vi.fn();
  fireEvent.click(preview);
  expect(audio.play).toHaveBeenCalledOnce();
  await waitFor(() => expect(preview.getAttribute("aria-label")).toMatch(/Приостановить|Призупинити/));
  fireEvent.pause(audio);
  expect(preview.getAttribute("aria-label")).toMatch(/Прослушать|Прослухати/);
  fireEvent.play(audio);
  expect(preview.getAttribute("aria-label")).toMatch(/Приостановить|Призупинити/);
  fireEvent.ended(audio);
  expect(preview.getAttribute("aria-label")).toMatch(/Прослушать|Прослухати/);
  const titleLabel = title.closest(".ui-text-field").querySelector("label");
  const artistLabel = artist.closest(".ui-text-field").querySelector("label");
  expect(titleLabel.textContent.trim()).not.toBe("");
  expect(artistLabel.textContent.trim()).not.toBe("");
  expect(titleLabel.textContent).not.toBe(artistLabel.textContent);
  expect(title.required).toBe(true);
  expect(title.value).toBe("Track");
  expect(artist.value).toBe("Artist");
  fireEvent.change(title, { target: { value: "New title" } });

  fireEvent.change(artist, { target: { value: "New artist" } });

  fireEvent.click(view.getByText(/Авто ·/));
  fireEvent.click(await view.findByRole("option", { name: /Быстрый|Швидкий/ }));

  fireEvent.click(view.getByRole("button", { name: /Подтвердить|Підтвердити/ }));
  await waitFor(() => expect(onConfirm).toHaveBeenCalledWith({ title: "New title", artist: "New artist", processingMode: "fast" }));
  fireEvent.click(view.getByText("Пропустить"));
  expect(onCancel).toHaveBeenCalledOnce();
  view.unmount();
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:Artist - Track.flac");
});

test("selected file preview stays inert without a usable object URL", () => {
  const missingFile = render(<SelectedFilePreview />);
  expect(missingFile.getByRole("button").disabled).toBe(true);
  expect(URL.createObjectURL).not.toHaveBeenCalled();
  missingFile.unmount();

  URL.createObjectURL = undefined;
  const unsupportedBrowser = render(<SelectedFilePreview file={new File(["audio"], "song.mp3")} />);
  expect(unsupportedBrowser.getByRole("button").disabled).toBe(true);
});

test("selected file preview replaces and releases its object URL", () => {
  const view = render(<SelectedFilePreview file={new File(["first"], "first.mp3")} />);
  const audio = view.container.querySelector("audio");
  audio.pause = vi.fn();
  expect(audio.src).toContain("blob:first.mp3");
  view.rerender(<SelectedFilePreview file={new File(["second"], "second.mp3")} />);
  expect(audio.src).toContain("blob:second.mp3");
  expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:first.mp3");
});
