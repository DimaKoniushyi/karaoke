// getPositionSec is a callback, not a plain value, so the instrumental's
// position is sampled right before recorder.start() -- as close as possible
// to the actual first captured sample -- instead of whatever it was back
// when the caller first invoked this function. The gap between those two
// moments used to go unaccounted for entirely (the WebAudio setup below,
// gated behind an async context.resume() only when there are 2+ remote
// participants, made that gap both real and inconsistent between the 1 vs
// 2+ participant cases), silently shifting remote vocals relative to the
// instrumental in the final mix.
export async function createRoomVoiceCapture(streams, getPositionSec) {
  const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
  const MediaRecorderClass = globalThis.MediaRecorder;
  const live = (Array.isArray(streams) ? streams : []).filter((stream) =>
    stream?.getAudioTracks?.().some((track) => track.readyState === "live")
  );
  if (!live.length || !MediaRecorderClass) return null;
  let context = null;
  let destination = null;
  let sources = [];
  let captureStream = live[0];
  if (live.length > 1) {
    if (!AudioContextClass) return null;
    context = new AudioContextClass({ latencyHint: "interactive" });
    destination = context.createMediaStreamDestination();
    sources = live.map((stream) => {
      const source = context.createMediaStreamSource(stream);
      source.connect(destination);
      return source;
    });
    await context.resume?.();
    captureStream = destination.stream;
  }
  const candidates = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus"];
  const mimeType = candidates.find((type) => MediaRecorderClass.isTypeSupported?.(type));
  const recorder = new MediaRecorderClass(captureStream, mimeType ? { mimeType } : undefined);
  const chunks = [];
  recorder.addEventListener("dataavailable", ({ data }) => {
    if (data?.size) chunks.push(data);
  });
  const positionAtStart =
    typeof getPositionSec === "function" ? getPositionSec() : Number(getPositionSec) || 0;
  recorder.start(500);
  return {
    startPlaybackSec: Math.max(0, Number(positionAtStart) || 0),
    pause: () => recorder.state === "recording" && recorder.pause(),
    resume: () => recorder.state === "paused" && recorder.resume(),
    stop: () =>
      new Promise((resolve) => {
        let settled = false;
        const finish = async () => {
          if (settled) return;
          settled = true;
          sources.forEach((source) => source.disconnect?.());
          destination?.stream.getTracks?.().forEach((track) => track.stop());
          await Promise.resolve(context?.close?.()).catch(() => {});
          resolve(
            chunks.length
              ? new Blob(chunks, { type: recorder.mimeType || mimeType || "audio/webm" })
              : null
          );
        };
        if (recorder.state === "inactive") finish();
        else {
          recorder.addEventListener("stop", finish, { once: true });
          // A recorder that errors instead of stopping, or one whose "stop"
          // event never arrives for some other reason, must not hang the
          // caller (finalizing the take) forever -- fall back to whatever
          // chunks were already captured instead of waiting indefinitely.
          recorder.addEventListener("error", finish, { once: true });
          setTimeout(finish, 5000);
          try {
            recorder.stop();
          } catch {
            finish();
          }
        }
      })
  };
}
