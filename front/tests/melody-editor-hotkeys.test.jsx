/* @vitest-environment jsdom */
import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import useEditorHotkeys from "../src/pages/MelodyEditor/useEditorHotkeys.js";

afterEach(cleanup);

function baseProps(overrides = {}) {
  return {
    save: vi.fn(),
    redo: vi.fn(),
    undo: vi.fn(),
    notes: [],
    copy: vi.fn(),
    remove: vi.fn(),
    paste: vi.fn(),
    duplicate: vi.fn(),
    transportRef: { current: { playing: false, play: vi.fn(), pause: vi.fn(), seek: vi.fn() } },
    duration: 10,
    selected: [],
    setSelected: vi.fn(),
    selectedWords: [],
    setSelectedWords: vi.fn(),
    shiftWords: vi.fn(),
    selectAdjacent: vi.fn(),
    nudge: vi.fn(),
    ...overrides
  };
}

function pressUndo(target) {
  target.dispatchEvent(
    new KeyboardEvent("keydown", { code: "KeyZ", ctrlKey: true, bubbles: true, cancelable: true })
  );
}

test("still runs a hotkey while a toolbar button keeps focus", () => {
  const props = baseProps();
  renderHook(() => useEditorHotkeys(props));
  const button = document.createElement("button");
  document.body.append(button);
  button.focus();

  pressUndo(button);

  expect(props.undo).toHaveBeenCalledOnce();
  button.remove();
});

test("blocks a hotkey while a text field owns focus", () => {
  const props = baseProps();
  renderHook(() => useEditorHotkeys(props));
  const input = document.createElement("input");
  document.body.append(input);
  input.focus();

  pressUndo(input);

  expect(props.undo).not.toHaveBeenCalled();
  input.remove();
});

test("blocks a hotkey while the role=slider playhead owns focus", () => {
  const props = baseProps();
  renderHook(() => useEditorHotkeys(props));
  const slider = document.createElement("div");
  slider.setAttribute("role", "slider");
  slider.tabIndex = 0;
  document.body.append(slider);
  slider.focus();

  pressUndo(slider);

  expect(props.undo).not.toHaveBeenCalled();
  slider.remove();
});
