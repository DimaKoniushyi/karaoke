/* @vitest-environment jsdom */
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { translateSaved } from "../src/i18n/runtime.js";
import { calledWith, verify } from "./helpers/assertions.mjs";
const mocks = vi.hoisted(() => ({
  updateAudioSettings: vi.fn(),
  releaseDirectMonitoring: vi.fn(),
  getAudioPreferences: vi.fn()
}));
vi.mock("../src/api/client", () => ({ api: mocks }));
vi.mock("../src/hooks/useAsyncQueue", () => ({ default: () => ({ run: (action) => action() }) }));
vi.mock("../src/utils/audio-preferences", () => ({
  getAudioPreferences: mocks.getAudioPreferences
}));
import useAudioOutputRouting from "../src/pages/Karaoke/hooks/useAudioOutputRouting.js";
import useMicrophoneSettings from "../src/pages/Karaoke/hooks/useMicrophoneSettings.js";
import { createOutputDeviceOptions } from "../src/utils/audio-devices.js";
beforeEach(() => {
  Object.values(mocks).forEach((mock) => mock.mockReset());
  mocks.releaseDirectMonitoring.mockResolvedValue(undefined);
  mocks.getAudioPreferences.mockReturnValue({ monitorInputDeviceId: "default" });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
describe("audio output device choices", () => {
  test("keeps active host endpoints and removes legacy duplicates and mappers", () => {
    const devices = [
      { index: 0, name: "Microsoft Sound Mapper - Output [MME]", host_api: "MME", max_output_channels: 2 },
      { index: 1, name: "Speakers [MME]", host_api: "MME", max_output_channels: 2 },
      { index: 2, name: "Speakers [Windows WASAPI]", host_api: "Windows WASAPI", max_output_channels: 2 },
      { index: 3, name: "HDMI [Windows WASAPI]", host_api: "Windows WASAPI", max_output_channels: 2 },
      { index: 4, name: "Disabled [Windows WASAPI]", host_api: "Windows WASAPI", max_output_channels: 0 },
      { index: 5, name: "Kernel output [Windows WDM-KS]", host_api: "Windows WDM-KS", max_output_channels: 2 }
    ];

    expect(createOutputDeviceOptions(devices, "", "auto", "System")).toEqual([
      { value: "", label: "System" },
      { value: 2, label: "Speakers" },
      { value: 3, label: "HDMI" }
    ]);
  });

  test("uses only ASIO outputs while ASIO mode is active", () => {
    const devices = [
      { index: 1, name: "Interface [MME]", host_api: "MME", max_output_channels: 2 },
      { index: 2, name: "Interface [ASIO]", host_api: "ASIO", max_output_channels: 2 },
      { index: 3, name: "Monitor [ASIO]", host_api: "ASIO", max_output_channels: 2 }
    ];

    expect(createOutputDeviceOptions(devices, 1, "asio", "System")).toEqual([
      { value: "", label: "System" },
      { value: 2, label: "Interface" },
      { value: 3, label: "Monitor" }
    ]);
  });

  test("uses only MME outputs while MME mode is active", () => {
    const devices = [
      { index: 1, name: "Interface [MME]", host_api: "MME", max_output_channels: 2 },
      { index: 2, name: "Interface [Windows WASAPI]", host_api: "Windows WASAPI", max_output_channels: 2 },
      { index: 3, name: "Monitor [MME]", host_api: "MME", max_output_channels: 2 }
    ];

    expect(createOutputDeviceOptions(devices, 1, "mme", "System")).toEqual([
      { value: "", label: "System" },
      { value: 1, label: "Interface" },
      { value: 3, label: "Monitor" }
    ]);
  });
});
describe("microphone settings", () => {
  test("normalizes backend settings and reacts to global changes", () => {
    const onError = vi.fn();
    const removeEventListener = vi.spyOn(globalThis, "removeEventListener");
    const hook = renderHook(({ settings }) => useMicrophoneSettings({ audioSettings: settings, onError }), {
      initialProps: {
        settings: {
          volume: 0.6,
          audio_driver: "asio",
          output_device_id: 3,
          monitoring_enabled: true,
          reverb: 0.2,
          echo: 0.3,
          delay: 0.4
        }
      }
    });
    verify([
      hook.result.current,
      "toMatchObject",
      {
        microphoneVolume: 0.6,
        audioDriver: "asio",
        directOutputDeviceId: 3,
        monitoringEnabled: true,
        microphoneEffects: { reverb: 0.2, echo: 0.3, delay: 0.4 }
      }
    ]);
    act(() =>
      window.dispatchEvent(
        new CustomEvent("audio-settings-changed", {
          detail: {
            volume: 0.2,
            audio_driver: "auto",
            output_device_id: 4,
            monitoring_enabled: false
          }
        })
      )
    );
    verify([
      hook.result.current,
      "toMatchObject",
      {
        microphoneVolume: 0.2,
        audioDriver: "auto",
        directOutputDeviceId: 4,
        monitoringEnabled: false
      }
    ]);
    hook.rerender({
      settings: { volume: 0.9, audio_driver: "asio", output_device_id: 8, monitoring_enabled: true }
    });
    verify([
      hook.result.current,
      "toMatchObject",
      {
        microphoneVolume: 0.9,
        audioDriver: "asio",
        directOutputDeviceId: 8,
        monitoringEnabled: true
      }
    ]);
    expect(hook.result.current.microphoneEffects).toEqual({
      reverb: 0.2,
      echo: 0.3,
      delay: 0.4,
      noise_suppression: 0.35,
      octave: 0
    });
    act(() => window.dispatchEvent(new CustomEvent("audio-settings-changed")));
    expect(hook.result.current.microphoneVolume).toBe(0.9);
    hook.rerender({ settings: null });
    verify([
      hook.result.current,
      "toMatchObject",
      {
        microphoneVolume: 0.9,
        audioDriver: "asio",
        directOutputDeviceId: 8,
        monitoringEnabled: true
      }
    ]);
    hook.unmount();
    verify([removeEventListener, "toHaveBeenCalledWith", "audio-settings-changed", expect.any(Function)]);
  });
  test("updates settings and reports backend failures", async () => {
    const onError = vi.fn();
    mocks.updateAudioSettings.mockResolvedValueOnce({ volume: 0.75 });
    const { result } = renderHook(() => useMicrophoneSettings({ audioSettings: null, onError }));
    verify([
      result.current,
      "toMatchObject",
      {
        microphoneVolume: 1,
        microphoneEffects: { reverb: 0, echo: 0, delay: 0, noise_suppression: 0.35, octave: 0 },
        audioDriver: "auto",
        directOutputDeviceId: "",
        monitoringEnabled: false,
        monitorInputDeviceId: "default"
      }
    ]);
    await act(() => result.current.updateMicrophone({ volume: 0.7 }));
    expect(result.current.microphoneVolume).toBe(0.75);
    mocks.updateAudioSettings.mockRejectedValueOnce(new Error("device busy"));
    await expect(result.current.updateMicrophone({ volume: 0.4 })).resolves.toBeNull();
    verify([onError, "toHaveBeenCalledWith", expect.stringContaining("device busy")]);
    mocks.updateAudioSettings.mockResolvedValueOnce({ echo: 0.2 });
    await act(() => result.current.updateMicrophone({ echo: 0.2 }));
    expect(result.current.microphoneVolume).toBe(0);
  });
  test("uses the latest error callback and the unknown-error fallback", async () => {
    const firstError = vi.fn();
    const nextError = vi.fn();
    const hook = renderHook(({ onError }) => useMicrophoneSettings({ audioSettings: null, onError }), {
      initialProps: { onError: firstError }
    });
    hook.rerender({ onError: nextError });
    mocks.updateAudioSettings.mockRejectedValueOnce({});
    await expect(hook.result.current.updateMicrophone({ volume: 0.5 })).resolves.toBeNull();
    expect(firstError).not.toHaveBeenCalled();
    expect(nextError).toHaveBeenCalledOnce();
    verify([nextError.mock.calls[0][0], "toContain", translateSaved("неизвестная ошибка")]);
  });
  test("effect mutations roll back on failure and preserve a newer queued value", async () => {
    const onError = vi.fn();
    const hook = renderHook(() =>
      useMicrophoneSettings({
        audioSettings: { reverb: 0.1, echo: 0.2, delay: 0, noise_suppression: 0.35, octave: 0 },
        onError
      })
    );
    mocks.updateAudioSettings.mockRejectedValueOnce(new Error("device busy"));
    await act(async () => {
      await hook.result.current.updateMicrophoneEffects({ echo: 0.8 });
    });
    expect(hook.result.current.microphoneEffects.echo).toBe(0.2);

    mocks.updateAudioSettings
      .mockRejectedValueOnce(new Error("obsolete failure"))
      .mockResolvedValueOnce({ echo: 0.7 });
    let first;
    let second;
    await act(async () => {
      first = hook.result.current.updateMicrophoneEffects({ echo: 0.4 });
      second = hook.result.current.updateMicrophoneEffects({ echo: 0.7 });
      await Promise.all([first, second]);
    });
    expect(hook.result.current.microphoneEffects.echo).toBe(0.7);
    expect(mocks.updateAudioSettings).toHaveBeenLastCalledWith({ echo: 0.7 });
    expect(onError).toHaveBeenCalledTimes(2);
  });
  test("ignores microphone updates that settle after unmount", async () => {
    let resolveUpdate;
    mocks.updateAudioSettings.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveUpdate = resolve;
      })
    );
    const resolved = renderHook(() => useMicrophoneSettings({ audioSettings: null, onError: vi.fn() }));
    const success = resolved.result.current.updateMicrophone({ volume: 0.2 });
    resolved.unmount();
    resolveUpdate({ volume: 0.3 });
    await expect(success).resolves.toEqual({ volume: 0.3 });
    let rejectUpdate;
    const onError = vi.fn();
    mocks.updateAudioSettings.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectUpdate = reject;
      })
    );
    const rejected = renderHook(() => useMicrophoneSettings({ audioSettings: null, onError }));
    const failure = rejected.result.current.updateMicrophone({ volume: 0.2 });
    rejected.unmount();
    rejectUpdate(new Error("obsolete"));
    await expect(failure).resolves.toBeNull();
    expect(onError).not.toHaveBeenCalled();
  });
});
const mediaRef = (setSinkId = vi.fn().mockResolvedValue(undefined)) => ({ current: { setSinkId } });
describe("audio output routing", () => {
  test("selects an ASIO output and routes browser media to the matching sink", async () => {
    const roomRoute = vi.fn();
    window.addEventListener("audio-output-route-changed", roomRoute);
    const setDirectOutputDeviceId = vi.fn();
    const updateMicrophone = vi.fn().mockRejectedValue(new Error("backend"));
    const instrumentalRef = mediaRef(vi.fn().mockRejectedValue(new Error("sink")));
    const vocalsRef = mediaRef();
    const videoRef = mediaRef();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        enumerateDevices: vi.fn().mockResolvedValue([{ kind: "audiooutput", deviceId: "browser-output", label: "Focusrite USB" }])
      }
    });
    const options = {
      audioDriver: "asio",
      audioSettings: { asio_driver_name: "Focusrite ASIO" },
      directOutputDeviceId: "",
      directOutputDevices: [{ index: 2, name: "Focusrite USB", is_asio: true }],
      instrumentalRef,
      microphoneOpen: true,
      setDirectOutputDeviceId,
      updateMicrophone,
      videoRef,
      vocalsRef
    };
    const hook = renderHook((props) => useAudioOutputRouting(props), { initialProps: options });
    calledWith([setDirectOutputDeviceId, [2]], [updateMicrophone, [{ output_device_id: 2 }]]);
    hook.rerender({ ...options, directOutputDeviceId: 2 });
    await waitFor(() => expect(instrumentalRef.current.setSinkId).toHaveBeenCalledWith("browser-output"));
    calledWith([vocalsRef.current.setSinkId, ["browser-output"]], [videoRef.current.setSinkId, ["browser-output"]]);
    expect(roomRoute).toHaveBeenCalledWith(expect.objectContaining({ detail: { deviceId: "browser-output" } }));
    window.removeEventListener("audio-output-route-changed", roomRoute);
  });
  test("selects an ASIO output only when it is needed and available", () => {
    const setDirectOutputDeviceId = vi.fn();
    const updateMicrophone = vi.fn();
    const base = {
      audioDriver: "auto",
      audioSettings: null,
      directOutputDeviceId: "",
      directOutputDevices: [{ index: 2, name: "Focusrite", is_asio: true }],
      instrumentalRef: { current: null },
      setDirectOutputDeviceId,
      updateMicrophone,
      videoRef: { current: null },
      vocalsRef: { current: null }
    };
    const hook = renderHook((props) => useAudioOutputRouting(props), { initialProps: base });
    expect(setDirectOutputDeviceId).not.toHaveBeenCalled();
    hook.rerender({
      ...base,
      audioDriver: "asio",
      audioSettings: { asio_driver_name: "Focusrite ASIO", output_device_id: "" }
    });
    expect(setDirectOutputDeviceId).toHaveBeenCalledExactlyOnceWith(2);
    expect(updateMicrophone).toHaveBeenCalledExactlyOnceWith({ output_device_id: 2 });
    setDirectOutputDeviceId.mockClear();
    updateMicrophone.mockClear();
    hook.rerender({
      ...base,
      audioDriver: "asio",
      audioSettings: { asio_driver_name: "Focusrite ASIO", output_device_id: 7 }
    });
    expect(setDirectOutputDeviceId).not.toHaveBeenCalled();
    hook.rerender({
      ...base,
      audioDriver: "asio",
      audioSettings: { asio_driver_name: "Unknown ASIO" },
      directOutputDevices: []
    });
    expect(setDirectOutputDeviceId).not.toHaveBeenCalled();
    hook.rerender({
      ...base,
      audioDriver: "asio",
      audioSettings: { asio_driver_name: "Focusrite ASIO" },
      directOutputDeviceId: "2"
    });
    expect(setDirectOutputDeviceId).not.toHaveBeenCalled();
  });
  test("does not enumerate without a usable device selection and API", () => {
    const enumerateDevices = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { enumerateDevices }
    });
    const base = {
      audioDriver: "auto",
      audioSettings: {},
      directOutputDevices: [],
      instrumentalRef: { current: null },
      setDirectOutputDeviceId: vi.fn(),
      updateMicrophone: vi.fn(),
      videoRef: { current: null },
      vocalsRef: { current: null }
    };
    const nullSelection = renderHook(() =>
      useAudioOutputRouting({
        ...base,
        directOutputDeviceId: null,
        directOutputDevices: [{ index: null, name: "Null" }]
      })
    );
    const emptySelection = renderHook(() =>
      useAudioOutputRouting({
        ...base,
        directOutputDeviceId: "",
        directOutputDevices: [{ index: "", name: "Empty" }]
      })
    );
    expect(enumerateDevices).not.toHaveBeenCalled();
    nullSelection.unmount();
    emptySelection.unmount();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
    verify([
      () =>
        renderHook(() =>
          useAudioOutputRouting({
            ...base,
            directOutputDeviceId: 2,
            directOutputDevices: [{ index: 2, name: "Output" }]
          })
        ),
      "not.toThrow"
    ]);
  });
  test("releases backend monitoring on shutdown", async () => {
    mocks.releaseDirectMonitoring.mockRejectedValueOnce(new Error("release"));
    const hook = renderHook(() =>
      useAudioOutputRouting({
        audioDriver: "auto",
        audioSettings: {},
        directOutputDeviceId: "",
        directOutputDevices: [],
        instrumentalRef: { current: null },
        microphoneOpen: false,
        setDirectOutputDeviceId: vi.fn(),
        updateMicrophone: vi.fn(),
        videoRef: { current: null },
        vocalsRef: { current: null }
      })
    );
    window.dispatchEvent(new Event("pagehide"));
    expect(mocks.releaseDirectMonitoring).toHaveBeenCalledOnce();
    hook.unmount();
    window.dispatchEvent(new Event("pagehide"));
    expect(mocks.releaseDirectMonitoring).toHaveBeenCalledOnce();
    await act(async () => Promise.resolve());
  });
  test("releases backend monitoring when leaving karaoke through client-side navigation", () => {
    const hook = renderHook(() =>
      useAudioOutputRouting({
        audioDriver: "auto",
        audioSettings: {},
        directOutputDeviceId: "",
        directOutputDevices: [],
        instrumentalRef: { current: null },
        microphoneOpen: false,
        setDirectOutputDeviceId: vi.fn(),
        updateMicrophone: vi.fn(),
        videoRef: { current: null },
        vocalsRef: { current: null }
      })
    );
    hook.unmount();
    expect(mocks.releaseDirectMonitoring).toHaveBeenCalledOnce();
  });
  test("isolates browser device enumeration failures", async () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { enumerateDevices: vi.fn().mockRejectedValue(new Error("devices")) }
    });
    renderHook(() =>
      useAudioOutputRouting({
        audioDriver: "auto",
        audioSettings: {},
        directOutputDeviceId: 2,
        directOutputDevices: [{ index: 2, name: "Output" }],
        instrumentalRef: { current: null },
        setDirectOutputDeviceId: vi.fn(),
        updateMicrophone: vi.fn(),
        videoRef: { current: null },
        vocalsRef: { current: null }
      })
    );
    await act(async () => Promise.resolve());
    expect(navigator.mediaDevices.enumerateDevices).toHaveBeenCalled();
  });
  test("ignores missing outputs, unmatched sinks and late enumeration", async () => {
    let resolveDevices;
    const enumerateDevices = vi.fn(
      () =>
        new Promise((resolve) => {
          resolveDevices = resolve;
        })
    );
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { enumerateDevices }
    });
    const routedAfterMissing = vi.fn().mockResolvedValue(undefined);
    const base = {
      audioDriver: "auto",
      audioSettings: {},
      directOutputDevices: [{ index: 2, name: "Output" }],
      instrumentalRef: { current: null },
      setDirectOutputDeviceId: vi.fn(),
      updateMicrophone: vi.fn(),
      videoRef: { current: { setSinkId: routedAfterMissing } },
      vocalsRef: { current: {} }
    };
    const missing = renderHook(() => useAudioOutputRouting({ ...base, directOutputDeviceId: 3 }));
    expect(enumerateDevices).not.toHaveBeenCalled();
    missing.unmount();
    const noDevices = renderHook(() => useAudioOutputRouting({ ...base, directOutputDeviceId: 2, directOutputDevices: undefined }));
    noDevices.unmount();
    const late = renderHook(() => useAudioOutputRouting({ ...base, directOutputDeviceId: 2 }));
    late.unmount();
    resolveDevices([{ kind: "audiooutput", deviceId: "late", label: "Output" }]);
    await act(async () => Promise.resolve());
    expect(routedAfterMissing).not.toHaveBeenCalled();
    enumerateDevices.mockResolvedValueOnce([]);
    renderHook(() => useAudioOutputRouting({ ...base, directOutputDeviceId: 2 }));
    await act(async () => Promise.resolve());
    enumerateDevices.mockResolvedValueOnce([{ kind: "audiooutput", deviceId: "sink", label: "Output" }]);
    renderHook(() => useAudioOutputRouting({ ...base, directOutputDeviceId: 2 }));
    await act(async () => Promise.resolve());
    expect(routedAfterMissing).toHaveBeenCalledWith("sink");
  });
});
