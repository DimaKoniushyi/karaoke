/* @vitest-environment jsdom */
import { act, renderHook } from "@testing-library/react";
import { beforeEach, expect, test, vi } from "vitest";

import advancedRows from "../src/pages/Settings/rows/advanced";
import useSettings from "../src/pages/Settings/use-settings";

const state = vi.hoisted(() => ({
  settings: { online_name: "Old", theme: "dark" },
  update: vi.fn(),
  save: vi.fn(),
  updateAudio: vi.fn(),
  startMonitor: vi.fn(),
  stopMonitor: vi.fn()
}));
vi.mock("../src/hooks/useAppSettings", () => ({
  default: () => ({ settings: state.settings, updateSettings: state.update })
}));
vi.mock("../src/api/client", () => ({
  api: {
    updateAppSettings: state.save,
    updateAudioSettings: state.updateAudio,
    startDirectMonitoring: state.startMonitor,
    stopDirectMonitoring: state.stopMonitor
  }
}));
vi.mock("../src/hooks/usePolling", () => ({
  usePolling: () => ({ data: null, refresh: vi.fn() })
}));
vi.mock("../src/contexts/AppDialog", () => ({ useAppDialog: () => ({ alert: vi.fn() }) }));
vi.mock("../src/contexts/radio", () => ({ useRadio: () => ({}) }));

beforeEach(() => vi.clearAllMocks());

test("reads the existing settings context and persists only the changed key", async () => {
  state.save.mockResolvedValue({ online_name: "", theme: "stale theme" });
  const { result } = renderHook(() => useSettings(false));
  expect(result.current.app.form).toBe(state.settings);
  await act(async () => result.current.app.save("online_name", ""));
  expect(state.save).toHaveBeenCalledWith({ online_name: "" });
  expect(state.update.mock.calls[0][0]({ online_name: "Old", theme: "green" })).toEqual({
    online_name: "",
    theme: "green"
  });
});

test("keyboard lighting persists false and zero without replacing them with null", async () => {
  state.save.mockResolvedValue({
    keyboard_lighting_enabled: false,
    keyboard_lighting_brightness: 0
  });
  const { result } = renderHook(() => useSettings(false));
  await act(async () => {
    await result.current.app.save("keyboard_lighting_enabled", false);
    await result.current.app.save("keyboard_lighting_brightness", 0);
  });
  expect(state.save).toHaveBeenCalledWith({ keyboard_lighting_enabled: false });
  expect(state.save).toHaveBeenCalledWith({ keyboard_lighting_brightness: 0 });
});

test("remote diagnostic consent is represented by universal settings fields", () => {
  const disabled = advancedRows({
    open: vi.fn(),
    settings: { app: { form: { remote_diagnostics_enabled: false } } },
    tr: (key) => key
  });
  const fields = disabled.filter(({ tag }) => tag);
  expect(fields.map(({ tag }) => tag)).toEqual([
    "remote_diagnostics_enabled",
    "remote_diagnostics_errors_enabled",
    "remote_diagnostics_hardware_enabled",
    "remote_crash_reports_enabled"
  ]);
  expect(fields.slice(1).every(({ disabled: value }) => value)).toBe(true);
  expect(fields.every(({ label }, index) => label === `settings.advanced.${fields[index].tag}.label`)).toBe(true);
});

test("queued writes ignore stale responses and recover after a rejected request", async () => {
  state.save.mockResolvedValueOnce({ online_name: "One" }).mockResolvedValueOnce({ online_name: "Two" });
  const { result } = renderHook(() => useSettings(false));
  await act(async () => {
    await Promise.all([result.current.app.save("online_name", "One"), result.current.app.save("online_name", "Two")]);
  });
  expect(state.update).toHaveBeenCalledOnce();
  expect(state.update.mock.calls[0][0]({}).online_name).toBe("Two");
  state.save.mockRejectedValueOnce(new Error("Offline"));
  await expect(result.current.app.save("online_name", "Three")).rejects.toThrow("Offline");
  state.save.mockResolvedValueOnce({ online_name: "Four" });
  await act(async () => result.current.app.save("online_name", "Four"));
  expect(state.update.mock.lastCall[0]({}).online_name).toBe("Four");
});

test("selectDriver maps the dropdown's three modes to distinct backend values", async () => {
  const { result } = renderHook(() => useSettings(false));

  state.updateAudio.mockResolvedValueOnce({ audio_driver: "auto", asio_driver_name: "" });
  await act(async () => result.current.audio.selectDriver(""));
  expect(state.updateAudio).toHaveBeenLastCalledWith({ audio_driver: "auto", asio_driver_name: "" });

  // The MME sentinel is a reserved, non-empty stand-in for asio_driver_name
  // (see use-settings.js) so the dropdown can visually distinguish "mme"
  // from "auto", which otherwise both bind to an empty asio_driver_name.
  state.updateAudio.mockResolvedValueOnce({ audio_driver: "mme", asio_driver_name: "mme" });
  await act(async () => result.current.audio.selectDriver("mme"));
  expect(state.updateAudio).toHaveBeenLastCalledWith({ audio_driver: "mme", asio_driver_name: "mme" });

  state.updateAudio.mockResolvedValueOnce({
    audio_driver: "asio",
    asio_driver_name: "Focusrite USB ASIO"
  });
  await act(async () => result.current.audio.selectDriver("Focusrite USB ASIO"));
  expect(state.updateAudio).toHaveBeenLastCalledWith({
    audio_driver: "asio",
    asio_driver_name: "Focusrite USB ASIO"
  });
});

test("settings monitoring starts a dry path without effects", async () => {
  state.startMonitor.mockResolvedValueOnce({ monitoring_enabled: true });
  const { result } = renderHook(() => useSettings(false));

  await act(async () => result.current.audio.monitor());

  expect(state.startMonitor).toHaveBeenCalledOnce();
  expect(state.startMonitor).toHaveBeenCalledWith({ disabledEffects: true });
  expect(state.stopMonitor).not.toHaveBeenCalled();
});
