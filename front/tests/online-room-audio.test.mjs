import { afterEach, expect, test, vi } from "vitest";
import { playParticipantJoinedSound, playParticipantLeftSound } from "../src/contexts/onlineRoomChime.js";
import { notCalled, verify } from "./helpers/assertions.mjs";
afterEach(() => {
  vi.restoreAllMocks();
  delete globalThis.AudioContext;
  delete globalThis.webkitAudioContext;
});
test("closes the participant notification AudioContext when playback actually ends", async () => {
  const oscillators = [];
  const close = vi.fn().mockResolvedValue(undefined);
  const gain = {
    gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
    connect: vi.fn()
  };
  globalThis.AudioContext = class {
    constructor() {
      this.currentTime = 10;
      this.destination = {};
      this.close = close;
    }
    createGain() {
      return gain;
    }
    createOscillator() {
      const oscillator = {
        connect: vi.fn(),
        frequency: { value: 0 },
        onended: null,
        start: vi.fn(),
        stop: vi.fn()
      };
      oscillators.push(oscillator);
      return oscillator;
    }
  };
  const timeout = vi.spyOn(globalThis, "setTimeout");
  playParticipantJoinedSound();
  expect(oscillators).toHaveLength(2);
  notCalled(timeout, close);
  expect(oscillators[0].onended).toBeTypeOf("function");
  expect(oscillators[1].onended).toBeTypeOf("function");
  oscillators[0].onended();
  oscillators[1].onended();
  await Promise.resolve();
  await Promise.resolve();
  verify(
    [close, "toHaveBeenCalledTimes", 1],
    [oscillators[0].onended, "toBeNull"],
    [oscillators[1].onended, "toBeNull"]
  );
  expect(playParticipantLeftSound()).toBe(true);
  expect(oscillators).toHaveLength(4);
  expect(oscillators[2].frequency.value).toBe(659.25);
});
test("notification sound stays optional when Web Audio is unavailable or fails", () => {
  expect(playParticipantJoinedSound()).toBe(false);
  globalThis.AudioContext = class {
    constructor() {
      throw new Error("audio unavailable");
    }
  };
  expect(playParticipantJoinedSound()).toBe(false);
});
