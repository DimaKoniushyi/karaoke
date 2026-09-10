/* @vitest-environment jsdom */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { same, sameDeep, notCalled, calledTimes, calledWith, verify } from "./helpers/assertions.mjs";
const importUtility = async (name) => {
  vi.resetModules();
  switch (name) {
    case "audio-preferences":
      return import("../src/utils/audio-preferences.js");
    case "clipboard":
      return import("../src/utils/clipboard.js");
    case "ui-preferences":
      return import("../src/utils/ui-preferences.js");
    default:
      throw new Error(`Unknown utility module: ${name}`);
  }
};
beforeEach(() => {
  localStorage.clear();
  delete window.electronAPI;
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
  delete document.execCommand;
});
afterEach(() => vi.restoreAllMocks());
describe("audio and UI preferences", () => {
  test("normalizes persisted audio preferences", () => {
    return importUtility("audio-preferences").then(({ DEFAULT_AUDIO_PREFERENCES, getAudioPreferences }) => {
      verify([DEFAULT_AUDIO_PREFERENCES, "toEqual", { monitorInputDeviceId: "default" }]);
      expect(getAudioPreferences()).toEqual(DEFAULT_AUDIO_PREFERENCES);
      localStorage.setItem("karaoke-audio-preferences", JSON.stringify({ monitorInputDeviceId: "mic" }));
      verify([getAudioPreferences(), "toEqual", { monitorInputDeviceId: "mic" }]);
    });
  });
  test("audio preferences reject malformed device ids and preserve omitted values", async () => {
    const { getAudioPreferences, saveAudioPreferences } = await importUtility("audio-preferences");
    localStorage.setItem("karaoke-audio-preferences", JSON.stringify({ monitorInputDeviceId: "mic-a" }));
    verify([
      saveAudioPreferences({ monitorInputDeviceId: "   " }),
      "toEqual",
      { monitorInputDeviceId: "default" }
    ]);
    expect(saveAudioPreferences(null)).toEqual(getAudioPreferences());
    verify([
      saveAudioPreferences({ monitorInputDeviceId: 123 }),
      "toEqual",
      { monitorInputDeviceId: "default" }
    ]);
  });
  test("saves device preferences and ignores obsolete controls", async () => {
    const { DEFAULT_AUDIO_PREFERENCES, saveAudioPreferences } = await importUtility("audio-preferences");
    const saved = saveAudioPreferences({
      monitorInputDeviceId: "usb",
      monitorMode: "browser",
      monitorLatencyHint: "playback"
    });
    verify([
      saved,
      "toEqual",
      {
        ...DEFAULT_AUDIO_PREFERENCES,
        monitorInputDeviceId: "usb"
      }
    ]);
    expect(JSON.parse(localStorage.getItem("karaoke-audio-preferences"))).toEqual(saved);
  });
  test("hydrates remote values and uploads local-only values", async () => {
    const { hydrateUiPreferences, UI_PREFERENCE_STORAGE } = await importUtility("ui-preferences");
    verify([
      UI_PREFERENCE_STORAGE,
      "toEqual",
      {
        audio: "karaoke-audio-preferences",
        karaoke: "karaoke-player-preferences",
        melody_editor: "karaoke-melody-editor",
        radio: "karaoke-radio",
        settings: "karaoke-settings-view"
      }
    ]);
    localStorage.setItem(UI_PREFERENCE_STORAGE.karaoke, JSON.stringify({ volume: 0.7 }));
    const api = {
      getUiPreferences: vi.fn().mockResolvedValue({
        audio: { monitorInputDeviceId: "remote-mic" },
        karaoke: null,
        radio: []
      }),
      updateUiPreferences: vi.fn().mockResolvedValue({})
    };
    await hydrateUiPreferences(api);
    verify([JSON.parse(localStorage.getItem(UI_PREFERENCE_STORAGE.audio)), "toEqual", { monitorInputDeviceId: "remote-mic" }]);
    verify(
      [api.updateUiPreferences, "toHaveBeenCalledWith", "karaoke", { volume: 0.7 }],
      [api.updateUiPreferences, "toHaveBeenCalledTimes", 1]
    );
    api.getUiPreferences.mockResolvedValueOnce(undefined);
    await expect(hydrateUiPreferences(api)).resolves.toBeUndefined();
    const callable = () => {};
    callable.invalid = true;
    api.getUiPreferences.mockResolvedValueOnce({ audio: callable });
    await hydrateUiPreferences(api);
    verify([JSON.parse(localStorage.getItem(UI_PREFERENCE_STORAGE.audio)), "toEqual", { monitorInputDeviceId: "remote-mic" }]);
    api.getUiPreferences.mockResolvedValueOnce({ audio: "x" });
    await hydrateUiPreferences(api);
    verify([JSON.parse(localStorage.getItem(UI_PREFERENCE_STORAGE.audio)), "toEqual", { monitorInputDeviceId: "remote-mic" }]);
  });
  test("hydrates every namespace independently and ignores arrays as remote objects", async () => {
    const { hydrateUiPreferences, UI_PREFERENCE_STORAGE } = await importUtility("ui-preferences");
    localStorage.setItem(UI_PREFERENCE_STORAGE.audio, JSON.stringify({ local: "audio" }));
    localStorage.setItem(UI_PREFERENCE_STORAGE.radio, JSON.stringify({ local: "radio" }));
    const api = {
      getUiPreferences: vi.fn().mockResolvedValue({
        audio: [],
        karaoke: { volume: 0.4 },
        melody_editor: { zoom: 12 },
        radio: null,
        settings: { tab: "ai" }
      }),
      updateUiPreferences: vi.fn().mockResolvedValue({})
    };
    await hydrateUiPreferences(api);
    calledWith([api.updateUiPreferences, ["audio", { local: "audio" }]], [api.updateUiPreferences, ["radio", { local: "radio" }]]);
    sameDeep(
      [JSON.parse(localStorage.getItem(UI_PREFERENCE_STORAGE.karaoke)), { volume: 0.4 }],
      [JSON.parse(localStorage.getItem(UI_PREFERENCE_STORAGE.melody_editor)), { zoom: 12 }],
      [JSON.parse(localStorage.getItem(UI_PREFERENCE_STORAGE.settings)), { tab: "ai" }]
    );
    expect(api.updateUiPreferences).toHaveBeenCalledTimes(2);
  });
  test("delegates persistence through the API transport", async () => {
    const { persistUiPreferences } = await importUtility("ui-preferences");
    const api = { updateUiPreferences: vi.fn().mockResolvedValue({}) };
    persistUiPreferences(api, "settings", { tab: "audio" });
    await Promise.resolve();
    expect(api.updateUiPreferences).toHaveBeenCalledWith("settings", { tab: "audio" });
  });
  test("persists known namespaces and still sends unknown ones", async () => {
    const { persistUiPreferences, UI_PREFERENCE_STORAGE } = await importUtility("ui-preferences");
    const api = { updateUiPreferences: vi.fn().mockRejectedValue(new Error("optional")) };
    const setItem = vi.spyOn(Storage.prototype, "setItem");
    expect(persistUiPreferences(api, "settings", { tab: "audio" })).toEqual({ tab: "audio" });
    verify([JSON.parse(localStorage.getItem(UI_PREFERENCE_STORAGE.settings)), "toEqual", { tab: "audio" }]);
    persistUiPreferences(api, "future", { enabled: true });
    await Promise.resolve();
    calledTimes([api.updateUiPreferences, 2], [setItem, 1]);
  });
});
describe("clipboard fallbacks", () => {
  test("rejects empty values and prefers Electron", async () => {
    const { copyText } = await importUtility("clipboard");
    document.execCommand = vi.fn(() => true);
    verify([await copyText(null), "toBe", false], [document.execCommand, "not.toHaveBeenCalled"]);
    window.electronAPI = { copyText: vi.fn().mockResolvedValue(1) };
    verify([await copyText(42), "toBe", true], [window.electronAPI.copyText, "toHaveBeenCalledWith", "42"]);
  });
  test("Electron false is authoritative and does not fall through to browser strategies", async () => {
    const { copyText } = await importUtility("clipboard");
    window.electronAPI = { copyText: vi.fn().mockResolvedValue(false) };
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    document.execCommand = vi.fn(() => true);
    verify([await copyText("text"), "toBe", false], [window.electronAPI.copyText, "toHaveBeenCalledWith", "text"]);
    notCalled(writeText, document.execCommand);
  });
  test("falls back from Electron to the browser clipboard", async () => {
    const { copyText } = await importUtility("clipboard");
    window.electronAPI = { copyText: vi.fn().mockRejectedValue(new Error("ipc unavailable")) };
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    verify([await copyText("text"), "toBe", true], [writeText, "toHaveBeenCalledWith", "text"]);
  });
  test("uses and cleans up the legacy textarea fallback", async () => {
    const { copyText } = await importUtility("clipboard");
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) }
    });
    const createElement = vi.spyOn(document, "createElement");
    document.execCommand = vi.fn(() => true);
    verify(
      [await copyText("legacy"), "toBe", true],
      [document.execCommand, "toHaveBeenCalledWith", "copy"],
      [document.querySelector("textarea"), "toBeNull"]
    );
    const textarea = createElement.mock.results.find(({ value }) => value?.tagName === "TEXTAREA").value;
    same(
      [textarea.readOnly, true],
      [textarea.style.position, "fixed"],
      [textarea.style.opacity, "0"],
      [textarea.style.pointerEvents, "none"]
    );
    document.execCommand.mockImplementation(() => {
      throw new Error("blocked");
    });
    verify([await copyText("blocked"), "toBe", false], [document.querySelector("textarea"), "toBeNull"]);
  });
  test("legacy clipboard fallback is unavailable without a document", async () => {
    const { copyText } = await importUtility("clipboard");
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
    Object.defineProperty(globalThis, "document", { configurable: true, value: undefined });
    expect(await copyText("text")).toBe(false);
    Object.defineProperty(globalThis, "document", descriptor);
  });
});
