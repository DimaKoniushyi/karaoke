import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { createRoomVoiceCapture } from "../src/services/roomVoiceCapture.js";

class FakeRecorder {
  constructor(stream, options) {
    this.stream = stream;
    this.state = "recording";
    this.listeners = {};
    this.mimeType = options?.mimeType || "";
    FakeRecorder.instances.push(this);
  }
  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  start() {}
  stop() {
    this.state = "inactive";
  }
  pause() {
    this.state = "paused";
  }
  resume() {
    this.state = "recording";
  }
  emit(type, event = {}) {
    for (const handler of this.listeners[type] || []) handler(event);
  }
  static isTypeSupported() {
    return true;
  }
}
FakeRecorder.instances = [];

const liveStream = () => ({ getAudioTracks: () => [{ readyState: "live" }] });

beforeEach(() => {
  FakeRecorder.instances = [];
  vi.stubGlobal("MediaRecorder", FakeRecorder);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

test("resolves with the recorded blob once the recorder actually stops", async () => {
  const capture = await createRoomVoiceCapture([liveStream()], () => 12);
  expect(capture.startPlaybackSec).toBe(12);
  const recorder = FakeRecorder.instances[0];

  const stopping = capture.stop();
  recorder.emit("dataavailable", { data: { size: 4 } });
  recorder.emit("stop");

  expect(await stopping).not.toBeNull();
});

// Neither of these used to be listened for at all -- a recorder that errors
// out, or whose "stop" event never arrives for any other reason, left the
// caller (finalizing the take) awaiting a Promise that would never settle.
test("stop() resolves instead of hanging forever when the recorder errors out", async () => {
  const capture = await createRoomVoiceCapture([liveStream()], () => 0);
  const recorder = FakeRecorder.instances[0];

  const stopping = capture.stop();
  recorder.emit("error", new Event("error"));

  await expect(stopping).resolves.toBeNull();
});

test("stop() resolves after a timeout if neither stop nor error ever fires", async () => {
  vi.useFakeTimers();
  const capture = await createRoomVoiceCapture([liveStream()], () => 0);

  const stopping = capture.stop();
  await vi.advanceTimersByTimeAsync(5000);

  await expect(stopping).resolves.toBeNull();
});

test("stop() resolves even if recorder.stop() throws synchronously", async () => {
  const capture = await createRoomVoiceCapture([liveStream()], () => 0);
  const recorder = FakeRecorder.instances[0];
  recorder.stop = () => {
    throw new Error("already stopped");
  };

  await expect(capture.stop()).resolves.toBeNull();
});
