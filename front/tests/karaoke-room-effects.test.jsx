import { expect, test } from "vitest";
import { createRoomSyncChannel } from "../src/services/roomSyncChannel.js";

// useKaraokeRoomEffects no longer exists as its own hook: the effect-broadcast
// dedup it used to own now lives in useKaraokeAudio.js, built directly on
// this shared channel. That hook recreates a fresh channel whenever
// participants/room identity changes (see its
// `roomEffects.current = createRoomSyncChannel()` effect), which is why a
// membership change always forces one resend even with unchanged effects --
// reproduced here by creating a new channel mid-test instead of reusing one.
test("shouldSend deduplicates identical states but resends once after a fresh channel", () => {
  let channel = createRoomSyncChannel();
  const state = { volume: 1, echo: 0.2 };
  expect(channel.shouldSend(state)).toBe(true);
  expect(channel.shouldSend({ ...state })).toBe(false);

  channel = createRoomSyncChannel();
  expect(channel.shouldSend({ ...state })).toBe(true);

  const next = { volume: 0.5, echo: 0.2 };
  expect(channel.shouldSend(next)).toBe(true);
  expect(channel.shouldSend({ ...next })).toBe(false);
});
