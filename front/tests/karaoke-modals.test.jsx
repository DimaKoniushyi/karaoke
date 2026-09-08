/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import React from "react";
import { beforeEach, expect, test, vi } from "vitest";
import PerformanceAnalysisModal, { formatRecordingDate, getRecordingList } from "../src/pages/Karaoke/analysis-modal/index.jsx";
const mocks = vi.hoisted(() => ({
  runAnalysis: vi.fn(),
  deleteRecording: vi.fn(),
  confirm: vi.fn()
}));
vi.mock("../src/api/client", () => ({
  api: {
    runAnalysis: mocks.runAnalysis,
    deleteRecording: mocks.deleteRecording,
    getPerformanceFileUrl: (id) => `performance/${id}`
  }
}));
vi.mock("../src/contexts/AppDialog", () => ({ useAppDialog: () => ({ confirm: mocks.confirm }) }));
vi.mock("../src/theme/ui", async (importOriginal) => ({
  ...(await importOriginal()),
  Modal: ({ children, titleProps }) => (
    <section>
      <h1>{titleProps?.title}</h1>
      {titleProps?.actions}
      {children}
    </section>
  )
}));
vi.mock("../src/components/AudioPlayer", () => ({
  AudioPlayer: ({ src }) => <audio data-testid="audio" src={src} />
}));
beforeEach(() => {
  mocks.runAnalysis.mockReset();
  mocks.deleteRecording.mockReset().mockResolvedValue(undefined);
  mocks.confirm.mockReset().mockResolvedValue(true);
});
test("analysis modal renders normalized result and deletes recording", async () => {
  mocks.runAnalysis.mockResolvedValue({
    pitch_accuracy_percent: 88,
    mean_deviation_semitones: 0.4,
    sections: [{ accuracy_percent: 90 }, { accuracy_percent: 60 }]
  });
  const done = vi.fn();
  const deleted = vi.fn();
  const result = render(<PerformanceAnalysisModal recordingId="rec" onClose={vi.fn()} onDone={done} onDeleted={deleted} />);
  expect(result.container.textContent).toMatch(/Аналізуємо|Анализируем/);
  await waitFor(() => expect(result.getByTestId("audio")).not.toBeNull());
  await waitFor(() => expect(result.container.querySelector('[data-role="analysis-score"]')).not.toBeNull());
  fireEvent.click(result.getByRole("button", { name: /Готово/ }));
  expect(done).toHaveBeenCalled();
  fireEvent.click(result.container.querySelector('[data-role="delete-recording"]'));
  await waitFor(() => expect(mocks.deleteRecording).toHaveBeenCalledWith("rec"));
  expect(deleted).toHaveBeenCalled();
});
test("analysis recording carousel starts on the active recording without restarting analysis", async () => {
  mocks.runAnalysis.mockResolvedValue({ accuracy_percent: 75 });
  const view = render(
    <PerformanceAnalysisModal
      recordingId="second"
      recordings={[
        { id: "first", created_at: "2026-08-20T10:00:00Z" },
        { id: "second", created_at: "2026-08-21T10:00:00Z" },
        { id: "third", created_at: "2026-08-22T10:00:00Z" }
      ]}
      onClose={vi.fn()}
    />
  );
  await waitFor(() => expect(view.getByTestId("audio").getAttribute("src")).toBe("performance/second"));
  expect(mocks.runAnalysis).toHaveBeenCalledTimes(1);
  expect(mocks.runAnalysis).toHaveBeenCalledWith("second");
  const previous = view.getByLabelText(/Предыдущая|Попередня/);
  const next = view.getByLabelText(/Следующая|Наступна/);
  expect(previous.disabled).toBe(false);
  expect(next.disabled).toBe(false);
  expect(view.container.textContent).toMatch(/Запись 2 из 3|Запис 2 з 3/);
  expect(view.container.textContent).toMatch(/анализируется|аналізується/);
  fireEvent.click(previous);
  expect(view.getByTestId("audio").getAttribute("src")).toBe("performance/first");
  expect(previous.disabled).toBe(true);
  expect(view.container.textContent).toMatch(/продолжает выполняться|продовжує виконуватися/);
  expect(view.container.querySelector('[data-role="analysis-score"]')).toBeNull();
  expect(mocks.runAnalysis).toHaveBeenCalledTimes(1);
  fireEvent.click(next);
  expect(view.container.querySelector('[data-role="analysis-score"]')).not.toBeNull();
  fireEvent.click(next);
  expect(view.getByTestId("audio").getAttribute("src")).toBe("performance/third");
  expect(next.disabled).toBe(true);
  expect(mocks.runAnalysis).toHaveBeenCalledTimes(1);
});
test("analysis recording list keeps one canonical active entry", () => {
  expect(getRecordingList(null, "active")).toEqual([{ id: "active" }]);
  expect(getRecordingList([{ id: "first" }, null, { id: "first", created_at: "new" }], "active")).toEqual([
    { id: "first", created_at: "new" },
    { id: "active" }
  ]);
  expect(getRecordingList([{ id: "active", created_at: "kept" }], "active")).toEqual([{ id: "active", created_at: "kept" }]);
  expect(formatRecordingDate()).toMatch(/Запись исполнения|Запис виконання/);
  expect(formatRecordingDate("invalid")).toMatch(/Запись исполнения|Запис виконання/);
  expect(formatRecordingDate("2026-08-21T10:00:00Z")).not.toMatch(/Запись исполнения|Запис виконання/);
});
test("analysis carousel resets its view when a new recording starts analysis", async () => {
  mocks.runAnalysis.mockResolvedValue({ accuracy_percent: 80 });
  const recordings = [{ id: "first" }, { id: "second" }, { id: "third" }];
  const view = render(<PerformanceAnalysisModal recordingId="second" recordings={recordings} onClose={vi.fn()} />);
  await waitFor(() => expect(view.getByTestId("audio").getAttribute("src")).toBe("performance/second"));
  fireEvent.click(view.getByLabelText(/Предыдущая|Попередня/));
  expect(view.getByTestId("audio").getAttribute("src")).toBe("performance/first");
  view.rerender(<PerformanceAnalysisModal recordingId="third" recordings={recordings} onClose={vi.fn()} />);
  await waitFor(() => expect(mocks.runAnalysis).toHaveBeenLastCalledWith("third"));
  await waitFor(() => expect(view.getByTestId("audio").getAttribute("src")).toBe("performance/third"));
  expect(mocks.runAnalysis).toHaveBeenCalledTimes(2);
});
test("analysis modal reports analysis and deletion failures", async () => {
  const close = vi.fn();
  mocks.runAnalysis.mockRejectedValueOnce(new Error("analysis failed"));
  const result = render(<PerformanceAnalysisModal recordingId="bad" onClose={close} />);
  await waitFor(() => expect(result.getByRole("alert").textContent).toContain("analysis failed"));
  fireEvent.click(result.getByRole("button", { name: /Закрити|Закрыть/ }));
  expect(close).toHaveBeenCalled();
  cleanup();
  mocks.runAnalysis.mockResolvedValueOnce({ accuracy_percent: 50 });
  mocks.deleteRecording.mockRejectedValueOnce(new Error("delete failed"));
  const deletion = render(<PerformanceAnalysisModal recordingId="rec" onClose={vi.fn()} />);
  await waitFor(() => expect(deletion.getByTestId("audio")).not.toBeNull());
  fireEvent.click(deletion.container.querySelector('[data-role="delete-recording"]'));
  await waitFor(() => expect(deletion.getByRole("alert").textContent).toContain("delete failed"));
});
test("analysis deletion respects cancellation and stale modal lifetimes", async () => {
  mocks.runAnalysis.mockResolvedValue({ accuracy_percent: 50 });
  mocks.confirm.mockResolvedValueOnce(false);
  const cancelled = render(<PerformanceAnalysisModal recordingId="cancel" onClose={vi.fn()} />);
  await waitFor(() => expect(cancelled.getByTestId("audio")).not.toBeNull());
  fireEvent.click(cancelled.container.querySelector('[data-role="delete-recording"]'));
  await act(async () => Promise.resolve());
  expect(mocks.deleteRecording).not.toHaveBeenCalled();
  cancelled.unmount();
  let resolveConfirm;
  mocks.confirm.mockReturnValueOnce(
    new Promise((resolve) => {
      resolveConfirm = resolve;
    })
  );
  const staleConfirm = render(<PerformanceAnalysisModal recordingId="stale" onClose={vi.fn()} />);
  await waitFor(() => expect(staleConfirm.getByTestId("audio")).not.toBeNull());
  fireEvent.click(staleConfirm.container.querySelector('[data-role="delete-recording"]'));
  staleConfirm.unmount();
  await act(async () => resolveConfirm(true));
  let rejectDelete;
  mocks.confirm.mockResolvedValueOnce(true);
  mocks.deleteRecording.mockReturnValueOnce(
    new Promise((_resolve, reject) => {
      rejectDelete = reject;
    })
  );
  const staleDelete = render(<PerformanceAnalysisModal recordingId="stale-delete" onClose={vi.fn()} />);
  await waitFor(() => expect(staleDelete.getByTestId("audio")).not.toBeNull());
  fireEvent.click(staleDelete.container.querySelector('[data-role="delete-recording"]'));
  await act(async () => Promise.resolve());
  staleDelete.unmount();
  await act(async () => rejectDelete(new Error("late")));
});
test("analysis ignores stale requests and completed deletion after unmount", async () => {
  let resolveStrict;
  mocks.runAnalysis.mockReturnValueOnce(
    new Promise((resolve) => {
      resolveStrict = resolve;
    })
  );
  const strict = render(
    <React.StrictMode>
      <PerformanceAnalysisModal recordingId="strict" onClose={vi.fn()} />
    </React.StrictMode>
  );
  await act(async () => resolveStrict({ pitch_accuracy_percent: 50 }));
  strict.unmount();
  let rejectOld;
  mocks.runAnalysis
    .mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectOld = reject;
      })
    )
    .mockResolvedValueOnce({ pitch_accuracy_percent: 60 });
  const changed = render(<PerformanceAnalysisModal recordingId="old" onClose={vi.fn()} />);
  changed.rerender(<PerformanceAnalysisModal recordingId="new" onClose={vi.fn()} />);
  await act(async () => rejectOld(new Error("stale")));
  await waitFor(() => expect(changed.getByTestId("audio")).not.toBeNull());
  changed.unmount();
  let resolveDelete;
  mocks.runAnalysis.mockResolvedValueOnce({ pitch_accuracy_percent: 70 });
  mocks.deleteRecording.mockReturnValueOnce(
    new Promise((resolve) => {
      resolveDelete = resolve;
    })
  );
  const deleting = render(<PerformanceAnalysisModal recordingId="late-success" onClose={vi.fn()} onDeleted={vi.fn()} />);
  await waitFor(() => expect(deleting.getByTestId("audio")).not.toBeNull());
  fireEvent.click(deleting.container.querySelector('[data-role="delete-recording"]'));
  await act(async () => Promise.resolve());
  deleting.unmount();
  await act(async () => resolveDelete());
});
