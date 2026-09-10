import assert from "node:assert/strict";
import { test, vi } from "vitest";
import { getKaraokeHotkeyAction } from "../src/pages/Karaoke/hooks/useKaraokeHotkeys.js";
import { equal } from "./helpers/assertions.mjs";
const loadHotkeys = async () => {
  vi.resetModules();
  return import("../src/utils/hotkeys.js");
};
function installDocument(dialogs = []) {
  globalThis.document = {
    querySelectorAll: (selector) => {
      equal([selector, '[role="dialog"][aria-modal="true"]']);
      return dialogs;
    }
  };
}
test("space always controls karaoke inside the active scope", () => {
  installDocument();
  const scope = { isConnected: true };
  const input = { closest: () => ({ tagName: "INPUT" }) };
  const event = {
    code: "Space",
    target: input,
    defaultPrevented: false,
    isComposing: false,
    repeat: false
  };
  equal([getKaraokeHotkeyAction(event, scope), "toggle-playback"]);
});
test("space does not escape a modal outside karaoke", () => {
  const scope = { isConnected: true };
  installDocument([{ contains: () => false }]);
  equal([getKaraokeHotkeyAction({ code: "Space", target: null, defaultPrevented: false, isComposing: false, repeat: false }, scope), null]);
});
test("karaoke action mapping is exact and rejects cancelled events", () => {
  installDocument();
  const scope = { isConnected: true };
  const base = {
    target: { closest: () => null },
    defaultPrevented: false,
    isComposing: false,
    repeat: false
  };
  for (const [code, action] of [
    ["Space", "toggle-playback"],
    ["ArrowLeft", "seek-backward"],
    ["ArrowRight", "seek-forward"],
    ["Escape", "stop"]
  ])
    equal([getKaraokeHotkeyAction({ ...base, code }, scope), action]);
  equal(
    [getKaraokeHotkeyAction({ ...base, code: "KeyA" }, scope), null],
    [getKaraokeHotkeyAction({ ...base, code: "toString" }, scope), null],
    [getKaraokeHotkeyAction(null, scope), null]
  );
  for (const property of ["defaultPrevented", "isComposing", "repeat"])
    equal([getKaraokeHotkeyAction({ ...base, code: "Space", [property]: true }, scope), null]);
  equal([getKaraokeHotkeyAction({ ...base, code: "Space" }, { isConnected: false }), null]);
});
test("editable targets use the complete protected selector", async () => {
  const { isEditableHotkeyTarget } = await loadHotkeys();
  const expected = [
    "input",
    "select",
    "textarea",
    "button",
    "a[href]",
    '[contenteditable=""]',
    '[contenteditable="true"]',
    '[role="textbox"]',
    '[role="slider"]',
    '[data-hotkeys="off"]'
  ].join(", ");
  equal(
    [
      isEditableHotkeyTarget({
        closest: (selector) => {
          assert.equal(selector, expected);
          return {};
        }
      }),
      true
    ],
    [isEditableHotkeyTarget({ closest: () => null }), false],
    [isEditableHotkeyTarget({}), false],
    [isEditableHotkeyTarget(null), false]
  );
});
test("only the top dialog owns an active hotkey scope", async () => {
  const { isHotkeyScopeActive } = await loadHotkeys();
  const scope = { isConnected: true };
  equal([isHotkeyScopeActive(null), false], [isHotkeyScopeActive({ isConnected: false }), false]);
  installDocument([]);
  equal([isHotkeyScopeActive(scope), true]);
  installDocument([{ contains: (candidate) => candidate === scope }, { contains: () => false }]);
  equal([isHotkeyScopeActive(scope), false]);
  installDocument([{ contains: () => false }, { contains: (candidate) => candidate === scope }]);
  equal([isHotkeyScopeActive(scope), true]);
});
test("every browser cancellation signal suppresses a hotkey", async () => {
  const { shouldIgnoreHotkey } = await loadHotkeys();
  const scope = { isConnected: true };
  const event = {
    defaultPrevented: false,
    isComposing: false,
    repeat: false,
    target: { closest: () => null }
  };
  installDocument();
  equal([shouldIgnoreHotkey(event, scope), false], [shouldIgnoreHotkey(null, scope), true]);
  for (const property of ["defaultPrevented", "isComposing", "repeat"])
    equal([shouldIgnoreHotkey({ ...event, [property]: true }, scope), true]);
  equal(
    [shouldIgnoreHotkey({ ...event, target: { closest: () => ({}) } }, scope), true],
    [shouldIgnoreHotkey(event, { isConnected: false }), true]
  );
});
