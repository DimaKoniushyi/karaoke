/* @vitest-environment jsdom */
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { stubImmediateAnimationFrame } from "./helpers/browser.mjs";
import { notCalled, calledWith, verify } from "./helpers/assertions.mjs";
import { mockUseI18nWithValues } from "./helpers/mocks.mjs";
const mocks = vi.hoisted(() => ({
  roomValue: null,
  speakingValue: null,
  radioValue: null,
  copyText: vi.fn(),
  pending: false,
  run: vi.fn()
}));
vi.mock("../src/contexts/OnlineRoomContext", () => ({
  useOnlineRoom: () => mocks.roomValue,
  useOnlineRoomSpeaking: () => mocks.speakingValue
}));
vi.mock("../src/contexts/radio", () => ({ useRadio: () => mocks.radioValue }));
vi.mock("../src/utils/clipboard", () => ({ copyText: mocks.copyText }));
vi.mock("../src/hooks/useExclusiveAsyncAction", () => ({
  default: () => ({ pending: mocks.pending, run: mocks.run })
}));
vi.mock("../src/i18n", async (importOriginal) => ({
  ...(await importOriginal()),
  useI18n: mockUseI18nWithValues
}));
import { OnlineRoomDock } from "../src/components/OnlineRoomDock.jsx";
import { OnlineRoomModal } from "../src/pages/OnlineRoom/index.jsx";
import OnlineRoomParticipant from "../src/components/OnlineRoomParticipant.jsx";
import RoomRadioSync from "../src/components/RoomRadioSync.jsx";
const roomValue = (overrides = {}) => ({
  room: { id: "ABCD", selfId: "self", host: true },
  roomUi: {},
  participants: [
    { id: "self", name: "Alice", role: "host", micMuted: false },
    { id: "guest", name: "Bob", role: "guest", micMuted: false }
  ],
  microphoneMuted: false,
  roomSoundMuted: false,
  mutedPeople: new Set(),
  effectPeople: new Set(),
  voiceError: "",
  transferStatus: null,
  createRoom: vi.fn().mockResolvedValue("ABCD"),
  joinRoom: vi.fn().mockResolvedValue("ABCD"),
  leaveRoom: vi.fn(),
  requestMicrophoneAccess: vi.fn().mockResolvedValue(true),
  setMicrophoneMuted: vi.fn(),
  setRoomSoundMuted: vi.fn(),
  togglePersonMuted: vi.fn(),
  togglePersonEffects: vi.fn(),
  syncUi: vi.fn(),
  ...overrides
});
beforeEach(() => {
  mocks.roomValue = roomValue();
  mocks.speakingValue = { localSpeakingLevel: 0.7, speakingLevels: { guest: 0.5 } };
  mocks.radioValue = {
    isPlaying: false,
    stationId: "one",
    stations: [{ id: "one" }, { id: "two" }],
    volume: 0.1,
    setStation: vi.fn(),
    setVolume: vi.fn(),
    turnOff: vi.fn(),
    turnOn: vi.fn().mockResolvedValue(true)
  };
  mocks.copyText.mockReset().mockResolvedValue(true);
  mocks.pending = false;
  mocks.run.mockReset().mockImplementation((action) => action());
  stubImmediateAnimationFrame();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.replaceChildren();
});
describe("online room participants", () => {
  test("renders self controls, voice level and leave action", () => {
    const interval = vi.spyOn(globalThis, "setInterval");
    const props = {
      person: mocks.roomValue.participants[0],
      room: mocks.roomValue.room,
      localSpeakingLevel: 0.7,
      speakingLevel: 0,
      microphoneMuted: false,
      roomSoundMuted: false,
      isLocallyMuted: false,
      effectsEnabled: false,
      onLeave: vi.fn(),
      onSetMicrophoneMuted: vi.fn(),
      onSetRoomSoundMuted: vi.fn(),
      onSetEffectsLocked: vi.fn(),
      onTogglePersonMuted: vi.fn(),
      onTogglePersonEffects: vi.fn()
    };
    const view = render(<OnlineRoomParticipant {...props} />);
    const meter = screen.getByRole("meter", { name: /room.person.speaking/ });
    expect(meter.getAttribute("aria-valuenow")).toBe("70");
    expect(meter.querySelector("path")).not.toBeNull();
    expect(meter.querySelector("rect")).toBeNull();
    expect(interval).toHaveBeenCalledWith(expect.any(Function), 28);
    fireEvent.click(screen.getByLabelText("room.microphone.disable"));
    fireEvent.click(screen.getByLabelText("room.sound.disable"));
    fireEvent.click(screen.getByLabelText("room.effects.deny"));
    fireEvent.click(screen.getByLabelText("room.leave"));
    calledWith([props.onSetMicrophoneMuted, [true]], [props.onSetRoomSoundMuted, [true]]);
    expect(props.onLeave).toHaveBeenCalledOnce();
    expect(props.onSetEffectsLocked).toHaveBeenCalledWith(true);
    view.rerender(<OnlineRoomParticipant {...props} microphoneMuted roomSoundMuted />);
    verify([screen.getByLabelText("room.microphone.enable"), "not.toBeNull"], [screen.getByLabelText("room.sound.enable"), "not.toBeNull"]);
  });
  test("renders remote mute and effects controls", () => {
    const toggleMuted = vi.fn();
    const toggleEffects = vi.fn();
    const setEffects = vi.fn();
    render(
      <OnlineRoomParticipant
        person={{ id: "guest", name: "Bob", role: "guest", micMuted: true }}
        room={{ selfId: "self" }}
        localSpeakingLevel={0}
        speakingLevel={0.9}
        microphoneMuted={false}
        roomSoundMuted={false}
        isLocallyMuted
        effectsEnabled
        effectSettings={{ volume: 1.2, reverb: 0.2, echo: 0.3, delay: 0.4, noise_suppression: 0.5, octave: 0 }}
        onSetParticipantEffects={setEffects}
        onTogglePersonMuted={toggleMuted}
        onTogglePersonEffects={toggleEffects}
      />
    );
    fireEvent.click(screen.getByLabelText(/room.person.effects.disable/));
    fireEvent.mouseEnter(screen.getByLabelText(/room.person.effects.disable/).parentElement);
    expect(document.querySelectorAll(".ui-rotary-knob")).toHaveLength(6);
    const reverb = screen.getByRole("slider", { name: "karaoke.reverb" });
    fireEvent.change(reverb, { target: { value: "0.7" } });
    fireEvent.pointerUp(reverb);
    const octave = screen.getByRole("slider", { name: "karaoke.voiceOctave" });
    fireEvent.change(octave, { target: { value: "0.5" } });
    fireEvent.pointerUp(octave);
    fireEvent.click(screen.getByLabelText(/room.person.enable/));
    calledWith([toggleEffects, ["guest"]], [toggleMuted, ["guest"]]);
    expect(setEffects).toHaveBeenCalledWith("guest", expect.objectContaining({ reverb: 0.7 }));
    expect(setEffects).toHaveBeenCalledWith("guest", expect.objectContaining({ octave: 0.5 }));
    expect(document.querySelector("[data-speaking]")).toBeNull();
  });
});
describe("online room dock", () => {
  test("hides without a room", () => {
    mocks.roomValue = roomValue({ room: null });
    const { container } = render(<OnlineRoomDock />);
    expect(container.textContent).toBe("");
  });
  test("copies code, collapses and restores the panel", async () => {
    vi.useFakeTimers();
    render(<OnlineRoomDock />);
    fireEvent.click(screen.getByLabelText("room.copyCode"));
    await act(async () => Promise.resolve());
    expect(mocks.copyText).toHaveBeenCalledWith("ABCD");
    fireEvent.click(screen.getByLabelText("room.hidePanel"));
    expect(screen.getByLabelText("room.showPanel")).not.toBeNull();
    fireEvent.click(screen.getByLabelText("room.showPanel"));
    expect(screen.getByLabelText("room.hidePanel")).not.toBeNull();
    act(() => vi.advanceTimersByTime(1600));
    verify([screen.getByLabelText("room.copyCode").textContent, "not.toContain", "room.copied"]);
    vi.useRealTimers();
  });
  test("ignores failed copy and clears a pending copy timer on unmount", async () => {
    mocks.copyText.mockResolvedValueOnce(false).mockResolvedValueOnce(true);
    const view = render(<OnlineRoomDock />);
    fireEvent.click(screen.getByLabelText("room.copyCode"));
    await act(async () => Promise.resolve());
    verify([screen.getByLabelText("room.copyCode").textContent, "not.toContain", "room.copied"]);
    fireEvent.click(screen.getByLabelText("room.copyCode"));
    await act(async () => Promise.resolve());
    fireEvent.click(screen.getByLabelText("room.copyCode"));
    await act(async () => Promise.resolve());
    const clear = vi.spyOn(globalThis, "clearTimeout");
    view.unmount();
    expect(clear).toHaveBeenCalled();
  });
  test("shows microphone recovery and transfer states", async () => {
    mocks.roomValue = roomValue({
      voiceError: "Permission denied",
      transferStatus: { stage: "sending", percent: 42 }
    });
    const view = render(<OnlineRoomDock />);
    fireEvent.click(screen.getByText("room.allowMicrophone"));
    await act(async () => Promise.resolve());
    verify(
      [mocks.roomValue.requestMicrophoneAccess, "toHaveBeenCalled"],
      [screen.getByRole("progressbar").getAttribute("aria-valuenow"), "toBe", "42"]
    );
    mocks.roomValue = roomValue({ transferStatus: { stage: "error", error: "Network" } });
    view.rerender(<OnlineRoomDock />);
    verify([screen.queryByRole("progressbar"), "toBeNull"], [screen.getByText(/Network/), "not.toBeNull"]);
    mocks.pending = true;
    mocks.roomValue = roomValue({
      room: { id: "ABCD", selfId: "self", host: false },
      voiceError: "Permission denied",
      transferStatus: { stage: "error", error: "" }
    });
    view.rerender(<OnlineRoomDock />);
    verify(
      [screen.getByText("room.requestingMicrophone"), "not.toBeNull"],
      [screen.getByText(/room.transfer.unknownError/), "not.toBeNull"]
    );
  });
  test("warns a guest when no participant currently holds the host role", () => {
    // TASK 5.1: the host disconnecting isn't announced by a dedicated message —
    // it's derived from the participants list no longer containing a host, so
    // this must react purely to that list (works for both "host left" and,
    // later, "host reconnected").
    mocks.roomValue = roomValue({
      room: { id: "ABCD", selfId: "self", host: false },
      participants: [{ id: "guest", name: "Bob", role: "guest", micMuted: false }]
    });
    const view = render(<OnlineRoomDock />);
    expect(screen.getByText("room.hostLeft")).not.toBeNull();

    mocks.roomValue = roomValue({
      room: { id: "ABCD", selfId: "self", host: false },
      participants: [
        { id: "guest", name: "Bob", role: "guest", micMuted: false },
        { id: "host", name: "Alice", role: "host", micMuted: false }
      ]
    });
    view.rerender(<OnlineRoomDock />);
    expect(screen.queryByText("room.hostLeft")).toBeNull();
  });
  test("never warns the host themself even while alone in the room", () => {
    mocks.roomValue = roomValue({
      room: { id: "ABCD", selfId: "self", host: true },
      participants: [{ id: "self", name: "Alice", role: "host", micMuted: false }]
    });
    render(<OnlineRoomDock />);
    expect(screen.queryByText("room.hostLeft")).toBeNull();
  });
});
describe("online room modal", () => {
  test("creates a room and closes on success", async () => {
    const close = vi.fn();
    render(<OnlineRoomModal onlineName="Alice" onClose={close} />);
    fireEvent.click(screen.getByText("room.create"));
    await act(async () => Promise.resolve());
    verify([mocks.roomValue.createRoom, "toHaveBeenCalledWith", "Alice"], [close, "toHaveBeenCalled"]);
  });
  test("validates, normalizes and joins by code or Enter", async () => {
    const close = vi.fn();
    render(<OnlineRoomModal onlineName="Bob" onClose={close} />);
    fireEvent.click(screen.getByText("room.joinByCode"));
    const input = screen.getByLabelText("room.code");
    fireEvent.change(input, { target: { value: " ab-cd " } });
    fireEvent.submit(input.closest("form"));
    await act(async () => Promise.resolve());
    expect(mocks.roomValue.joinRoom).toHaveBeenCalledWith("AB-CD", "Bob");
    fireEvent.click(screen.getByText("room.back"));
  });
  test("shows connection failures", async () => {
    mocks.roomValue.createRoom.mockRejectedValue(new Error("socket refused"));
    render(<OnlineRoomModal onlineName="Alice" onClose={vi.fn()} />);
    fireEvent.click(screen.getByText("room.create"));
    await act(async () => Promise.resolve());
    expect(screen.getByText("socket refused")).not.toBeNull();
  });
  test("shows join failures and ignores connection completion after unmount", async () => {
    mocks.roomValue.joinRoom.mockRejectedValueOnce(new Error("join refused"));
    const failed = render(<OnlineRoomModal onlineName="Alice" onClose={vi.fn()} />);
    fireEvent.click(screen.getByText("room.joinByCode"));
    const input = screen.getByLabelText("room.code");
    fireEvent.change(input, { target: { value: "ABCD" } });
    fireEvent.click(screen.getByText("room.join"));
    await act(async () => Promise.resolve());
    expect(screen.getByText("join refused")).not.toBeNull();
    failed.unmount();
    let resolveCreate;
    mocks.roomValue.createRoom.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveCreate = resolve;
      })
    );
    const staleSuccess = render(<OnlineRoomModal onlineName="Alice" onClose={vi.fn()} />);
    fireEvent.click(screen.getByText("room.create"));
    staleSuccess.unmount();
    await act(async () => resolveCreate());
    let rejectCreate;
    mocks.roomValue.createRoom.mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        rejectCreate = reject;
      })
    );
    const staleFailure = render(<OnlineRoomModal onlineName="Alice" onClose={vi.fn()} />);
    fireEvent.click(screen.getByText("room.create"));
    staleFailure.unmount();
    await act(async () => rejectCreate(new Error("late")));
  });
  test("blocks short codes and repeated pending connections", async () => {
    let release;
    mocks.roomValue.createRoom.mockReturnValueOnce(
      new Promise((resolve) => {
        release = resolve;
      })
    );
    const close = vi.fn();
    render(<OnlineRoomModal onlineName="Alice" onClose={close} />);
    fireEvent.click(screen.getByText("room.joinByCode"));
    const join = screen.getByText("room.join");
    expect(join.disabled).toBe(true);
    fireEvent.keyDown(screen.getByLabelText("room.code"), { key: "Enter" });
    expect(mocks.roomValue.joinRoom).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("room.back"));
    const create = screen.getByText("room.create");
    fireEvent.click(create);
    fireEvent.click(create);
    await act(async () => Promise.resolve());
    expect(mocks.roomValue.createRoom).toHaveBeenCalledTimes(1);
    release();
    await act(async () => Promise.resolve());
  });
});
describe("room radio synchronization", () => {
  test("broadcasts local radio state", () => {
    render(<RoomRadioSync />);
    verify([mocks.roomValue.syncUi, "toHaveBeenCalledWith", { radio: { isPlaying: false, stationId: "one", volume: 0.1 } }]);
  });
  test("does not overwrite an already-present remote state on mount", () => {
    mocks.roomValue.roomUi = { __eventId: 10, radio: { stationId: "two", isPlaying: true } };
    render(<RoomRadioSync />);
    verify(
      [mocks.roomValue.syncUi, "not.toHaveBeenCalled"],
      [mocks.radioValue.setStation, "toHaveBeenCalledWith", "two", { resume: true }],
      [mocks.radioValue.turnOn, "toHaveBeenCalled"]
    );
  });
  test("applies remote station playback and stop", async () => {
    mocks.roomValue.roomUi = { __eventId: 1, radio: { stationId: "two", isPlaying: true } };
    const view = render(<RoomRadioSync />);
    expect(mocks.radioValue.setStation).toHaveBeenCalledWith("two", { resume: true });
    verify([mocks.radioValue.turnOn, "toHaveBeenCalledWith", expect.objectContaining({ remember: false, fadeIn: true })]);
    await act(async () => Promise.resolve());
    mocks.radioValue = { ...mocks.radioValue, stationId: "two", isPlaying: true };
    mocks.roomValue = roomValue({
      roomUi: { __eventId: 2, radio: { stationId: "two", isPlaying: false } }
    });
    view.rerender(<RoomRadioSync />);
    expect(mocks.radioValue.turnOff).toHaveBeenCalledWith({ remember: false });
  });
  test("ignores missing rooms and already-applied remote radio state", () => {
    mocks.roomValue = roomValue({ room: null });
    const view = render(<RoomRadioSync />);
    expect(mocks.roomValue.syncUi).not.toHaveBeenCalled();
    mocks.roomValue = roomValue({ roomUi: { radio: { stationId: "one", isPlaying: false } } });
    view.rerender(<RoomRadioSync />);
    notCalled(mocks.radioValue.turnOn, mocks.radioValue.turnOff);
  });
  test("uses station fallbacks without changing stopped playback", async () => {
    mocks.roomValue.roomUi = { __eventId: 3, radio: { stationId: "missing", isPlaying: true } };
    const view = render(<RoomRadioSync />);
    verify([mocks.radioValue.turnOn, "toHaveBeenCalledWith", expect.objectContaining({ targetStation: mocks.radioValue.stations[0] })]);
    await act(async () => Promise.resolve());
    mocks.radioValue = { ...mocks.radioValue, isPlaying: false };
    mocks.roomValue = roomValue({
      roomUi: { __eventId: 4, radio: { stationId: "two", isPlaying: false } }
    });
    view.rerender(<RoomRadioSync />);
    expect(mocks.radioValue.setStation).toHaveBeenCalledWith("two", { resume: false });
    mocks.roomValue = roomValue({ roomUi: { __eventId: 5, radio: { isPlaying: true } } });
    view.rerender(<RoomRadioSync />);
    expect(mocks.radioValue.turnOn).toHaveBeenCalled();
  });
});
