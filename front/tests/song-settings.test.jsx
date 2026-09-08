/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { verify } from "./helpers/assertions.mjs";
import { passthrough } from "./helpers/mocks.mjs";
const mocks = vi.hoisted(() => ({
  notify: vi.fn(),
  navigate: vi.fn(),
  updateSong: vi.fn()
}));
vi.mock("react-router-dom", () => ({ useNavigate: () => mocks.navigate }));
vi.mock("../src/contexts/AppDialog", () => ({ useAppDialog: () => ({ alert: mocks.notify }) }));
vi.mock("../src/api/client", () => ({ api: { updateSong: mocks.updateSong } }));
vi.mock("../src/theme/ui", async (importOriginal) => ({
  ...(await importOriginal()),
  Modal: ({ children, titleProps }) => (
    <section>
      <span data-testid="description">{titleProps?.description}</span>
      {titleProps?.actions}
      {children}
    </section>
  ),
  Button: ({ children, startIcon: _icon, ...props }) => <button {...props}>{children}</button>,
  Stack: passthrough("div"),
  Typography: passthrough("p")
}));
import SongSettings from "../src/pages/Library/modals/song-settings/index.jsx";
const song = {
  id: "song",
  title: "Title",
  artist: "Artist",
  status: "done",
  note_range_min: 50,
  note_range_max: 70
};
beforeEach(() => {
  mocks.notify.mockReset().mockResolvedValue(undefined);
  mocks.navigate.mockReset();
  mocks.updateSong.mockReset().mockResolvedValue({ title: "Updated" });
});
describe("song settings", () => {
  test("refreshing the same song does not replace an unsaved Formik draft", async () => {
    const saved = vi.fn();
    const view = render(<SongSettings song={song} onSaved={saved} />);
    const title = await view.findByRole("textbox", { name: /Название песни|Назва пісні/i });
    fireEvent.change(title, { target: { value: "Unsaved draft" } });
    view.rerender(<SongSettings song={{ ...song, title: "Server value" }} onSaved={saved} />);
    expect(title.value).toBe("Unsaved draft");
    fireEvent.click(view.getByRole("button", { name: /Сохранить|Зберегти/ }));
    await waitFor(() => expect(mocks.updateSong).toHaveBeenCalledWith("song", expect.objectContaining({ title: "Unsaved draft" })));
  });
  test("edits, validates and saves song fields", async () => {
    const saved = vi.fn();
    const result = render(<SongSettings song={song} onClose={vi.fn()} onSaved={saved} />);
    await waitFor(() => expect(result.container.querySelector("form")).not.toBeNull());
    fireEvent.change(result.getByRole("textbox", { name: /Название песни|Назва пісні/i }), { target: { value: "New title" } });
    const numericFields = [result.getByLabelText(/Нижняя нота|Нижня нота/), result.getByLabelText(/Верхняя нота|Верхня нота/)];
    fireEvent.change(numericFields[0], { target: { value: "48" } });
    fireEvent.change(numericFields[1], { target: { value: "72" } });
    fireEvent.change(numericFields[0], { target: { value: "" } });
    fireEvent.change(numericFields[0], { target: { value: "48" } });
    const save = result.getByRole("button", { name: /Сохранить|Зберегти/ });
    fireEvent.click(save);
    await waitFor(() => expect(mocks.updateSong).toHaveBeenCalled());
    expect(mocks.updateSong.mock.calls[0][0]).toBe("song");
    verify([mocks.updateSong.mock.calls[0][1], "toMatchObject", { title: "New title", note_range_min: 48, note_range_max: 72 }]);
    expect(saved).toHaveBeenCalled();
  });
  test("opens melody editor after closing settings", async () => {
    const close = vi.fn();
    const result = render(<SongSettings song={song} onClose={close} />);
    await waitFor(() => expect(result.container.querySelector("form")).not.toBeNull());
    const editor = result.getByRole("button", { name: /Открыть редактор|Відкрити редактор/ });
    fireEvent.click(editor);
    verify([close, "toHaveBeenCalled"], [mocks.navigate, "toHaveBeenCalledWith", "/editor/song"]);
  });
  test("renders the missing-song state", () => {
    const missing = render(<SongSettings song={null} />);
    expect(missing.getByRole("alert")).not.toBeNull();
  });
  test("reports validation and backend save errors", async () => {
    const result = render(<SongSettings song={song} />);
    await waitFor(() => expect(result.container.querySelector("form")).not.toBeNull());
    const title = result.getByRole("textbox", { name: /Название песни|Назва пісні/i });
    fireEvent.change(title, { target: { value: "" } });
    fireEvent.click(result.getByRole("button", { name: /Сохранить|Зберегти/ }));
    await waitFor(() => expect(mocks.notify).toHaveBeenCalled());
    mocks.updateSong.mockRejectedValueOnce(new Error("save failed"));
    fireEvent.change(title, { target: { value: "Valid" } });
    fireEvent.click(result.getByRole("button", { name: /Сохранить|Зберегти/ }));
    await waitFor(() => expect(mocks.notify.mock.calls.at(-1)[0]).toContain("save failed"));
  });
  test("accepts a successful save without a response payload", async () => {
    mocks.updateSong.mockResolvedValueOnce(null);
    const saved = vi.fn();
    const result = render(<SongSettings song={{ ...song, note_range_max: null }} onSaved={saved} />);
    await waitFor(() => expect(result.container.querySelector("form")).not.toBeNull());
    fireEvent.click(result.getByRole("button", { name: /Сохранить|Зберегти/ }));
    await waitFor(() => expect(saved).toHaveBeenCalled());
  });
  test("loads a different song when the selected id changes", async () => {
    const result = render(<SongSettings song={song} />);
    const title = await result.findByRole("textbox", { name: /Название песни|Назва пісні/i });
    result.rerender(<SongSettings song={{ ...song, id: "next", title: "Next" }} />);
    await waitFor(() => expect(title.value).toBe("Next"));
  });
});
