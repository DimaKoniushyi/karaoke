/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { verify } from "./helpers/assertions.mjs";
import { passthrough } from "./helpers/mocks.mjs";
const mocks = vi.hoisted(() => ({
  poll: {},
  notify: vi.fn(),
  navigate: vi.fn(),
  updateSong: vi.fn(),
  refresh: vi.fn()
}));
vi.mock("react-router-dom", () => ({ useNavigate: () => mocks.navigate }));
vi.mock("../src/contexts/AppDialog", () => ({ useAppDialog: () => ({ alert: mocks.notify }) }));
vi.mock("../src/hooks/usePolling", () => ({
  usePolling: () => ({ refresh: mocks.refresh, ...mocks.poll })
}));
vi.mock("../src/api/client", () => ({ api: { listSongs: vi.fn(), updateSong: mocks.updateSong } }));
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
  mocks.poll = { data: [song], error: null };
  mocks.notify.mockReset().mockResolvedValue(undefined);
  mocks.navigate.mockReset();
  mocks.refresh.mockReset().mockResolvedValue(undefined);
  mocks.updateSong.mockReset().mockResolvedValue({ title: "Updated" });
});
describe("song settings", () => {
  test("polling the same song does not replace an unsaved Formik draft", async () => {
    const view = render(<SongSettings songId="song" />);
    const title = await view.findByLabelText("Название песни", { exact: false });
    fireEvent.change(title, { target: { value: "Unsaved draft" } });
    mocks.poll = { data: [{ ...song, title: "Server value" }], error: null };
    view.rerender(<SongSettings songId="song" />);
    expect(title.value).toBe("Unsaved draft");
    fireEvent.click(view.getByRole("button", { name: "Сохранить" }));
    await waitFor(() => expect(mocks.updateSong).toHaveBeenCalledWith("song", expect.objectContaining({ title: "Unsaved draft" })));
  });
  test("edits, validates and saves song fields", async () => {
    const result = render(<SongSettings songId="song" onClose={vi.fn()} />);
    await waitFor(() => expect(result.getByTestId("form")).not.toBeNull());
    fireEvent.change(result.getByLabelText("Название песни", { exact: false }), { target: { value: "New title" } });
    const numericFields = [result.getByLabelText("Нижняя нота"), result.getByLabelText("Верхняя нота")];
    fireEvent.change(numericFields[0], { target: { value: "48" } });
    fireEvent.change(numericFields[1], { target: { value: "72" } });
    fireEvent.change(numericFields[0], { target: { value: "" } });
    fireEvent.change(numericFields[0], { target: { value: "48" } });
    const save = result.getByRole("button", { name: /Сохранить|Зберегти/ });
    fireEvent.click(save);
    await waitFor(() => expect(mocks.updateSong).toHaveBeenCalled());
    expect(mocks.updateSong.mock.calls[0][0]).toBe("song");
    verify([mocks.updateSong.mock.calls[0][1], "toMatchObject", { title: "New title", note_range_min: 48, note_range_max: 72 }]);
    expect(mocks.refresh).toHaveBeenCalled();
  });
  test("opens melody editor after closing settings", async () => {
    const close = vi.fn();
    const result = render(<SongSettings songId="song" onClose={close} />);
    await waitFor(() => expect(result.getByTestId("form")).not.toBeNull());
    const editor = result.getByRole("button", { name: "Открыть редактор" });
    fireEvent.click(editor);
    verify([close, "toHaveBeenCalled"], [mocks.navigate, "toHaveBeenCalledWith", "/editor/song"]);
  });
  test("renders loading, missing and request-error states", () => {
    mocks.poll = { data: null, error: null };
    const loading = render(<SongSettings songId="song" />);
    expect(loading.container.textContent).toMatch(/Загружаем|Завантажуємо/);
    cleanup();
    mocks.poll = { data: [], error: null };
    const missing = render(<SongSettings songId="song" />);
    expect(missing.getByRole("alert")).not.toBeNull();
    cleanup();
    mocks.poll = { data: null, error: new Error("offline") };
    const failed = render(<SongSettings songId="song" />);
    expect(failed.container.textContent).toContain("offline");
    fireEvent.click(failed.container.querySelector("button"));
    expect(mocks.refresh).toHaveBeenCalled();
  });
  test("reports validation and backend save errors", async () => {
    const result = render(<SongSettings songId="song" />);
    await waitFor(() => expect(result.getByTestId("form")).not.toBeNull());
    fireEvent.change(result.getByLabelText("Название песни", { exact: false }), { target: { value: "" } });
    fireEvent.click(result.getByRole("button", { name: /Сохранить|Зберегти/ }));
    await waitFor(() => expect(mocks.notify).toHaveBeenCalled());
    mocks.updateSong.mockRejectedValueOnce(new Error("save failed"));
    fireEvent.change(result.getByLabelText("Название песни", { exact: false }), { target: { value: "Valid" } });
    fireEvent.click(result.getByRole("button", { name: /Сохранить|Зберегти/ }));
    await waitFor(() => expect(mocks.notify.mock.calls.at(-1)[0]).toContain("save failed"));
  });
  test("accepts a successful save without a response payload", async () => {
    mocks.poll = { data: [{ ...song, note_range_max: null }], error: null };
    mocks.updateSong.mockResolvedValueOnce(null);
    const result = render(<SongSettings songId="song" />);
    await waitFor(() => expect(result.getByTestId("form")).not.toBeNull());
    fireEvent.click(result.getByRole("button", { name: /Сохранить|Зберегти/ }));
    await waitFor(() => expect(mocks.refresh).toHaveBeenCalled());
  });
  test("tracks a song appearing after the polling result changes", async () => {
    mocks.poll = { data: [], error: null };
    const result = render(<SongSettings songId="song" />);
    expect(result.getByRole("alert")).not.toBeNull();
    mocks.poll = { data: [song], error: null };
    await act(() => result.rerender(<SongSettings songId="song" />));
    await waitFor(() => expect(result.getByTestId("form")).not.toBeNull());
  });
});
