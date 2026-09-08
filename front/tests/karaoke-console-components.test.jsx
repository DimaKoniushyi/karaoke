/* @vitest-environment jsdom */
import { fireEvent, render } from "@testing-library/react";
import { afterEach, expect, test, vi } from "vitest";
import { sameDeep, called, calledWith, verify } from "./helpers/assertions.mjs";
import { passthrough } from "./helpers/mocks.mjs";
vi.mock("../src/theme/ui", () => ({
  Box: ({ as: Component = "div", sx: _sx, ...props }) => <Component {...props} />,
  RangeInput: ({ onChange, onCommit, ...props }) => (
    <input
      {...props}
      type="range"
      onChange={(event) => onChange?.(Number(event.target.value))}
      onPointerUp={(event) => onCommit?.(Number(event.currentTarget.value))}
    />
  ),
  RotaryKnob: ({ label, value, onChange }) => (
    <label className="karaoke-effect-dial">
      {label}
      <input type="range" aria-label={label} value={value} onChange={(event) => onChange(Number(event.target.value))} />
    </label>
  ),
  NumberField: ({ onChange, ...props }) => <input {...props} type="number" onChange={onChange} />,
  Slider: ({ label, value, onChange, onCommit, formatValue: _format, showValue: _show, sx, ...props }) => (
    <input
      {...props}
      type="range"
      aria-label={label}
      style={sx}
      value={value}
      onChange={(event) => onChange?.(Number(event.target.value))}
      onPointerUp={(event) => onCommit?.(Number(event.currentTarget.value))}
    />
  ),
  Switch: ({ label, checked, onChange, ...props }) => (
    <label>
      {label}
      <input
        {...props}
        type="checkbox"
        aria-label={label}
        checked={Boolean(checked)}
        onChange={(event) => onChange?.(event.target.checked)}
      />
    </label>
  ),
  Stack: passthrough("div"),
  Grid: passthrough("div"),
  Typography: passthrough("span"),
  Card: ({ children, surface: _surface, tilt: _tilt, sx: _sx, cardContent: _content, ...props }) => <div {...props}>{children}</div>,
  Button: ({ children, sx: _sx, startIcon: _startIcon, ...props }) => <button {...props}>{children}</button>,
  IconButton: ({ icon: Icon, sx: _sx, label, ...props }) => (
    <button aria-label={label} {...props}>
      {Icon ? <Icon /> : null}
    </button>
  ),
  Waveform: ({ onChange }) => <button data-testid="timeline" onClick={() => onChange(3)} />
}));
import RotaryKnob from "../src/theme/ui/RotaryKnob/index.jsx";
import { getRotaryDragValue, getRotaryPointerValue, getRotaryWheelValue } from "../src/theme/ui/RotaryKnob/utils.js";
import ConsoleCenter, { noteRangeLabel } from "../src/pages/Karaoke/console/center/index.jsx";
import MixerPanel from "../src/pages/Karaoke/console/mixer.jsx";
import SongStrip from "../src/pages/Karaoke/console/song-strip.jsx";
import ToolsPanel from "../src/pages/Karaoke/console/tools.jsx";
import KaraokeStageActions from "../src/pages/Karaoke/actions.jsx";
test("rotary calculations preserve VST sensitivity, direction and limits", () => {
  expect(getRotaryDragValue({ value: 0.5, lastY: 100, clientY: 10, min: 0, max: 1 })).toBe(1);
  expect(getRotaryDragValue({ value: 0.5, lastY: 100, clientY: 10, min: 0, max: 1, fine: true })).toBe(0.6);
  expect(getRotaryDragValue({ value: 0.5, lastY: 0, clientY: 900, min: 0, max: 1 })).toBe(0);
  expect(getRotaryWheelValue({ value: 0.5, deltaY: -1, step: 0.1, min: 0, max: 1 })).toBe(0.6);
  expect(getRotaryWheelValue({ value: 0.5, deltaY: 1, step: 0.1, min: 0, max: 1 })).toBe(0.4);
  expect(getRotaryWheelValue({ value: 0.5, deltaY: -1, step: 0.1, min: 0, max: 1, fine: true })).toBe(0.52);
  expect(getRotaryWheelValue({ value: 1, deltaY: -1, step: 0.1, min: 0, max: 1 })).toBe(1);
  expect(
    getRotaryPointerValue({
      clientX: 100,
      clientY: 50,
      rect: { left: 0, top: 0, width: 100, height: 100 },
      min: 0,
      max: 1
    })
  ).toBeCloseTo(5 / 6);
  const offsetRect = { left: 20, top: 10, width: 100, height: 100 };
  expect(
    getRotaryPointerValue({
      clientX: 20,
      clientY: 110,
      rect: offsetRect,
      min: 10,
      max: 20
    })
  ).toBe(10);
  expect(
    getRotaryPointerValue({
      clientX: 70,
      clientY: 10,
      rect: offsetRect,
      min: 10,
      max: 20
    })
  ).toBe(15);
  expect(
    getRotaryPointerValue({
      clientX: 120,
      clientY: 110,
      rect: offsetRect,
      min: 10,
      max: 20
    })
  ).toBe(20);
  expect(
    getRotaryPointerValue({
      clientX: 69,
      clientY: 110,
      rect: offsetRect,
      min: 10,
      max: 20
    })
  ).toBe(10);
  expect(
    getRotaryPointerValue({
      clientX: 71,
      clientY: 110,
      rect: offsetRect,
      min: 10,
      max: 20
    })
  ).toBe(20);
});
test("effect dial supports range, wheel and pointer dragging with clamping", () => {
  const change = vi.fn();
  const commit = vi.fn();
  const result = render(<RotaryKnob label="Echo" value={0.5} onChange={change} onCommit={commit} />);
  const { container, getByLabelText } = result;
  const dial = container.querySelector("label");
  fireEvent.pointerMove(dial, { clientY: 50, pointerId: 1 });
  fireEvent.wheel(dial, { deltaY: -1 });
  fireEvent.wheel(dial, { deltaY: 1 });
  fireEvent.pointerDown(dial, { button: 1, clientY: 100, pointerId: 1 });
  fireEvent.pointerDown(dial, { button: 0, clientY: 100, pointerId: 1 });
  fireEvent.pointerMove(dial, { clientY: 0, pointerId: 1 });
  fireEvent.pointerUp(dial, { pointerId: 1 });
  fireEvent.change(getByLabelText("Echo"), { target: { value: "0.3" } });
  verify(
    [change.mock.calls.length, "toBeGreaterThanOrEqual", 4],
    [change.mock.calls[0][0], "toBe", 0.55],
    [change.mock.calls[1][0], "toBe", 0.5],
    [change.mock.calls[2][0], "toBe", 1],
    [commit, "toHaveBeenCalled"]
  );
  result.unmount();
  render(<RotaryKnob label="Empty" onChange={vi.fn()} />);
});
test("rotary knob supports cumulative VST drag, fine adjustment and reset", () => {
  const change = vi.fn();
  const commit = vi.fn();
  const view = render(<RotaryKnob label="Reverb" value={0.5} defaultValue={0.25} onChange={change} onCommit={commit} />);
  const dial = view.container.querySelector("label");
  fireEvent.pointerDown(dial, { button: 0, clientY: 100, pointerId: 2 });
  fireEvent.pointerMove(dial, { clientY: 82, pointerId: 2 });
  fireEvent.pointerMove(dial, { clientY: 64, pointerId: 2, shiftKey: true });
  fireEvent.pointerUp(dial, { clientY: 64, pointerId: 2 });
  expect(change.mock.calls[0][0]).toBeCloseTo(0.6);
  expect(change.mock.calls[1][0]).toBeCloseTo(0.62);
  expect(commit).toHaveBeenLastCalledWith(0.62);
  fireEvent.doubleClick(view.container.querySelector(".karaoke-effect-dial__control"));
  expect(change).toHaveBeenLastCalledWith(0.25);
  expect(commit).toHaveBeenLastCalledWith(0.25);
});
test("rotary knob jumps to the clicked arc and accepts an exact percentage", () => {
  const change = vi.fn();
  const commit = vi.fn();
  const view = render(<RotaryKnob label="Noise" value={0.2} onChange={change} onCommit={commit} />);
  const control = view.container.querySelector(".karaoke-effect-dial__control");
  control.getBoundingClientRect = () => ({ left: 0, top: 0, width: 100, height: 100 });
  fireEvent.pointerDown(control, { button: 0, clientX: 100, clientY: 50, pointerId: 4 });
  fireEvent.pointerUp(view.container.querySelector("label"), { pointerId: 4 });
  expect(change).toHaveBeenLastCalledWith(expect.closeTo(5 / 6, 5));
  fireEvent.doubleClick(view.container.querySelector("strong"));
  const editor = view.getByRole("spinbutton", { name: "Noise, 20%" });
  fireEvent.change(editor, { target: { value: "75" } });
  fireEvent.keyDown(editor, { key: "Enter" });
  expect(change).toHaveBeenLastCalledWith(0.75);
  expect(commit).toHaveBeenLastCalledWith(0.75);
});
test("rotary knob displays amplified volume and remains inert while disabled", () => {
  const change = vi.fn();
  const commit = vi.fn();
  const view = render(
    <RotaryKnob
      label="Microphone"
      value={1.2}
      max={2}
      displayFactor={100}
      onChange={change}
      onCommit={commit}
    />
  );
  expect(view.container.querySelector("strong").textContent).toBe("120%");
  fireEvent.doubleClick(view.container.querySelector("strong"));
  const editor = view.getByRole("spinbutton", { name: "Microphone, 120%" });
  fireEvent.change(editor, { target: { value: "150" } });
  fireEvent.keyDown(editor, { key: "Enter" });
  expect(change).toHaveBeenLastCalledWith(1.5);
  expect(commit).toHaveBeenLastCalledWith(1.5);

  view.rerender(
    <RotaryKnob
      label="Microphone"
      value={1.2}
      max={2}
      displayFactor={100}
      disabled
      onChange={change}
      onCommit={commit}
    />
  );
  change.mockClear();
  commit.mockClear();
  fireEvent.change(view.getByRole("slider", { name: "Microphone" }), {
    target: { value: "1.5" }
  });
  expect(change).not.toHaveBeenCalled();
  expect(commit).not.toHaveBeenCalled();
});
test("song strip formats metadata and delegates seeking", () => {
  const seek = vi.fn();
  const result = render(<SongStrip song={{ title: "Song", performer: "Singer" }} currentTime={65} duration={130} onSeek={seek} />);
  verify([result.container.textContent, "toContain", "Singer"], [result.container.textContent, "toContain", "1:05"]);
  fireEvent.click(result.getByTestId("timeline"));
  expect(seek).toHaveBeenCalledWith(3);
  result.rerender(<SongStrip song={{ title: "Unknown" }} currentTime={0} duration={0} onSeek={seek} />);
});
test("mixer changes and commits volumes and effects", () => {
  const microphone = vi.fn();
  const commit = vi.fn();
  const effect = vi.fn();
  const monitoring = vi.fn();
  const view = render(
    <MixerPanel
      microphoneLevel={2}
      volumes={{ microphone: 0.4, music: 0.5, vocal: 0.6, melody: 0.7 }}
      onVolumeChange={{ microphone, music: vi.fn(), vocal: vi.fn(), melody: vi.fn() }}
      onVolumeCommit={{ microphone: commit, music: vi.fn(), vocal: vi.fn(), melody: vi.fn() }}
      microphoneEffects={{ echo: 0.1, reverb: 0.2, delay: 0.3 }}
      onEffectChange={effect}
      onEffectCommit={vi.fn()}
      monitoringEnabled
      onMonitoringChange={monitoring}
    />
  );
  const { container } = view;
  const slider = container.querySelector('input[type="range"]');
  // Rotated vertical sliders must claim touch/pen gestures exclusively, or a
  // drag on them can be handed to the page as a pan instead of changing the
  // value (the whole interface appears to drag along with the pointer).
  expect(slider.style.touchAction).toBe("none");
  fireEvent.change(slider, { target: { value: "0.8" } });
  fireEvent.pointerUp(slider, { currentTarget: { value: "0.8" } });
  fireEvent.keyUp(slider, { currentTarget: { value: "0.8" } });
  fireEvent.change(container.querySelector(".karaoke-effect-dial input"), {
    target: { value: "0.6" }
  });
  fireEvent.click(view.getByLabelText(/Чую себе|Слышу себя/));
  expect(microphone).toHaveBeenCalledWith(0.8);
  called(commit, effect);
  expect(monitoring).toHaveBeenCalledWith(false);
  view.rerender(
    <MixerPanel
      microphoneLevel={0}
      volumes={{}}
      onVolumeChange={{ microphone: vi.fn(), music: vi.fn(), vocal: vi.fn(), melody: vi.fn() }}
      onVolumeCommit={{ microphone: vi.fn(), music: vi.fn(), vocal: vi.fn(), melody: vi.fn() }}
      microphoneEffects={{}}
      onEffectChange={vi.fn()}
      onEffectCommit={vi.fn()}
    />
  );
});
test("tools toggle visibility, auto-hide, settings and presets", () => {
  const handlers = {
    onToggleNotes: vi.fn(),
    onToggleLyrics: vi.fn(),
    onAutoHideChange: vi.fn(),
    onOpenAppSettings: vi.fn(),
    onApplyEffectPreset: vi.fn()
  };
  const { container } = render(
    <ToolsPanel {...handlers} showNotes showLyrics={false} monitoringEnabled autoHideEnabled={false} effectPreset="hall" />
  );
  const toolButtons = container.querySelectorAll("div > button");
  for (const button of [...toolButtons].slice(0, 5)) fireEvent.click(button);
  const preset = [...container.querySelectorAll("button")].find((button) => button.title?.includes("%"));
  fireEvent.click(preset);
  called(handlers.onToggleNotes, handlers.onToggleLyrics);
  calledWith([handlers.onAutoHideChange, [true]]);
  called(handlers.onOpenAppSettings, handlers.onApplyEffectPreset);
});
test("center controls transport, tempo and bounded key changes", () => {
  const handlers = {
    onSkip: vi.fn(),
    onTogglePlay: vi.fn(),
    onStop: vi.fn(),
    onTempoChange: vi.fn(),
    onKeyShiftChange: vi.fn(),
    onLyricsOffsetChange: vi.fn()
  };
  const result = render(
    <ConsoleCenter
      {...handlers}
      song={{ note_range_min: "A2", note_range_max: "E5" }}
      currentTempo={128}
      compactKey="Dm"
      keyShift={12}
      lyricsOffset={-3}
      isPlaying={false}
    />
  );
  fireEvent.click(result.getByLabelText("Назад на 5 секунд"));
  fireEvent.click(result.getByLabelText("Вперёд на 5 секунд"));
  fireEvent.click(result.getByLabelText("Воспроизвести"));
  fireEvent.click(result.getByLabelText("Остановить"));
  fireEvent.click(result.getByLabelText("Уменьшить темп на 1 BPM"));
  fireEvent.click(result.getByLabelText("Увеличить темп на 1 BPM"));
  fireEvent.click(result.getByLabelText("Понизить тональность"));
  fireEvent.click(result.getByLabelText("Повысить тональность"));
  fireEvent.change(result.getByLabelText("Сдвиг слов в секундах"), {
    target: { value: "-2.9" }
  });
  expect(handlers.onSkip.mock.calls).toEqual([[-5], [5]]);
  expect(handlers.onTogglePlay).toHaveBeenCalledOnce();
  expect(handlers.onStop).toHaveBeenCalledOnce();
  sameDeep([handlers.onTempoChange.mock.calls, [[-1], [1]]], [handlers.onKeyShiftChange.mock.calls, [[11], [12]]]);
  expect(handlers.onLyricsOffsetChange).toHaveBeenCalledWith(-2.9);
  expect(result.container.textContent).toContain("A2 – E5");
});
test("center uses fallback note range and pause state", () => {
  const result = render(
    <ConsoleCenter
      song={{}}
      currentTempo={100}
      compactKey="C"
      keyShift={-12}
      isPlaying
      onSkip={vi.fn()}
      onTogglePlay={vi.fn()}
      onStop={vi.fn()}
      onTempoChange={vi.fn()}
      onKeyShiftChange={vi.fn()}
    />
  );
  fireEvent.click(result.getByLabelText("Понизить тональность"));
  verify([result.getByLabelText("Пауза"), "toBeTruthy"], [result.container.textContent, "toContain", "C2 – C5"]);
});
test("formats numeric MIDI range as musical note names", () => {
  expect(noteRangeLabel(46, "C2")).toBe("A♯2");
  expect(noteRangeLabel(73, "C5")).toBe("C♯5");
  expect(noteRangeLabel(60, "C2", 2)).toBe("D4");
  expect(noteRangeLabel(null, "C2")).toBe("C2");
});
test("hidden console always leaves a visible restore action", () => {
  const { getByLabelText } = render(
    <KaraokeStageActions
      controlsVisible={false}
      hideControls={vi.fn()}
      isPlaying
      isRadioPlaying={false}
      returnToLibrary={vi.fn()}
      sceneTransitioning={false}
      showControls={vi.fn()}
      stageActionsVisible={false}
      toggleRadio={vi.fn()}
    />
  );

  expect(getByLabelText(/консол/i)).toBeTruthy();
});
