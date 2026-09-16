/* @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import audioRows from "../src/pages/Settings/rows/audio";
import { RenderFormikFields, useGetForm } from "../src/theme/ui";

function AudioFields({ audio }) {
  const formik = useGetForm({ initialValues: { audio: audio.values } });
  return (
    <RenderFormikFields
      formik={formik}
      items={audioRows({ settings: { audio }, run: (action) => action() })}
      onFieldCommit={(name, value) => audio.update(name.slice(6), value)}
    />
  );
}
afterEach(cleanup);
const audio = (status = {}, rest = {}) => ({
  values: { monitoring_enabled: true, audio_driver: "auto", asio_driver_name: "", buffer_size: 128 },
  options: {
    drivers: [
      { value: "", label: "WASAPI shared" },
      { value: "Studio ASIO", label: "ASIO · Studio ASIO" }
    ]
  },
  selectDriver: vi.fn(),
  update: vi.fn(),
  monitor: vi.fn(),
  monitorStatus: { state: "running", host_api: "Windows WASAPI", mode: "shared", ...status },
  ...rest
});

test("starting is not displayed as working audio or zero latency", () => {
  render(<AudioFields audio={audio({ state: "starting" })} />);
  expect(screen.getByText("Подключаем микрофон…").getAttribute("role")).toBe("status");
  expect(screen.queryByText(/Задержка/)).toBeNull();
});

test("ASIO reports input, output and total with three decimal places", () => {
  render(
    <AudioFields
      audio={audio({
        mode: "ASIO",
        driver: "Studio ASIO",
        latency_source: "asio-driver-report",
        input_latency_ms: 130000 / 44100,
        output_latency_ms: 154000 / 44100
      })}
    />
  );
  expect(screen.getByText("Драйвер: Studio ASIO")).toBeTruthy();
  expect(screen.getByText("Задержка драйвера: 6.440 мс · вход 2.948 · выход 3.492").title).toContain("ASIOGetLatencies");
});

test("shared estimate is not labelled as a measurement; technical wall of text is removed", () => {
  render(
    <AudioFields
      audio={audio({
        input_latency_ms: 22,
        output_latency_ms: 24.9,
        latency_source: "portaudio-buffer-estimate",
        callback_frames: 128,
        glitch_count: 2,
        dsp_compute_ms: 0.083,
        queue_ms: 5,
        queue_capacity_ms: 12,
        input_device: "Microphone",
        output_device: "Speakers"
      })}
    />
  );
  expect(screen.getByText("Драйвер: Windows WASAPI · shared")).toBeTruthy();
  expect(screen.getByText("Задержка (оценка): 46.900 мс · вход 22.000 · выход 24.900").title).toContain("не физический замер");
  for (const text of [/Задержка драйвера/, /Последний блок/, /События сбоя/, /Время вычислений/, /очереди/, /Microphone →/])
    expect(screen.queryByText(text)).toBeNull();
});

test("native shared uses timestamp latency, not allocated buffer estimates", () => {
  render(
    <AudioFields
      audio={audio({
        latency_source: "wasapi-stream-report",
        stream_latency_ms: 22.6694,
        input_latency_ms: 22,
        output_latency_ms: 24.9
      })}
    />
  );
  // Device timestamps do not prove the acoustic mic-to-headphone delay.
  expect(screen.getByText("Оценка задержки (микрофон → выход): 22.669 мс").title).toContain(
    "Измерено по временным меткам аудиодрайвера"
  );
  expect(screen.queryByText(/46.900/)).toBeNull();
});

test("measured monitoring latency includes active effect history", () => {
  render(
    <AudioFields
      audio={audio({
        latency_source: "wasapi-stream-report",
        stream_latency_ms: 22.6694,
        effect_latency_ms: 6
      })}
    />
  );
  expect(screen.getByText("Оценка задержки (микрофон → выход): 28.669 мс").title).toContain(
    "алгоритмическую задержку активных эффектов"
  );
});

test.each([0, -1, null, NaN, Infinity])("invalid native clock latency stays unavailable: %s", (value) => {
  render(<AudioFields audio={audio({ latency_source: "wasapi-stream-report", stream_latency_ms: value })} />);
  expect(screen.getByText("Задержка: нет данных")).toBeTruthy();
  expect(screen.queryByText(/0.000 мс/)).toBeNull();
});

test.each([
  {},
  { input_latency_ms: -1, output_latency_ms: 3 },
  { input_latency_ms: null, output_latency_ms: 3 },
  { input_latency_ms: NaN, output_latency_ms: 3 },
  { input_latency_ms: 3, output_latency_ms: Infinity }
])("invalid latency does not become zero: %j", (status) => {
  render(<AudioFields audio={audio(status)} />);
  expect(screen.getByText("Задержка: нет данных")).toBeTruthy();
  expect(screen.queryByText(/0.000 мс/)).toBeNull();
});

test("fixed buffer keeps its existing behavior", async () => {
  const state = audio();
  render(<AudioFields audio={state} />);
  fireEvent.click(screen.getByRole("button", { name: "Аудиобуфер" }));
  fireEvent.click(screen.getByRole("option", { name: "256" }));
  await waitFor(() => expect(state.update).toHaveBeenCalledWith("buffer_size", 256));
});

test("driver selector offers ASIO and shared, no exclusive", async () => {
  const state = audio();
  render(<AudioFields audio={state} />);
  fireEvent.click(screen.getByRole("button", { name: "Режим звука" }));
  expect(screen.queryByRole("option", { name: /Эксклюзив/ })).toBeNull();
  fireEvent.click(screen.getByRole("option", { name: "ASIO · Studio ASIO" }));
  await waitFor(() => expect(state.selectDriver).toHaveBeenCalledWith("Studio ASIO"));
  expect(state.update).not.toHaveBeenCalled();
});

test("ASIO retains fixed buffer selection, not auto buffer", () => {
  render(<AudioFields audio={audio({}, { values: { audio_driver: "asio", asio_driver_name: "Studio ASIO" } })} />);
  expect(screen.getByRole("button", { name: "Аудиобуфер" })).toBeTruthy();
  expect(screen.queryByRole("switch", { name: /Автобуфер/ })).toBeNull();
});

test("polling and startup errors remain visible", () => {
  render(<AudioFields audio={audio({ state: "error", error: "Device busy" }, { monitorStatusError: true })} />);
  expect(screen.getByText("Device busy").getAttribute("role")).toBe("alert");
  expect(screen.getByText("Не удалось получить состояние микрофона")).toBeTruthy();
});
