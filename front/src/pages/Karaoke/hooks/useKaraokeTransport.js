import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { translateSaved as t } from "../../../i18n/runtime";
import { createRoomVoiceCapture } from "../../../services/roomVoiceCapture";
import { safe } from "../utils/async";
import { playbackGain } from "../utils/data";
import { finalizeRecording, formatError } from "../utils/recordingSession";
import { clampPlaybackPosition } from "../utils/transport";
import useKaraokeRecording from "./useKaraokeRecording";
import useKaraokeRoomTransport from "./useKaraokeRoomTransport";

export { createRoomVoiceCapture } from "../../../services/roomVoiceCapture";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
// The capture device's sample clock and the instrumental's playback clock are
// independent hardware clocks that drift apart over a take -- a single sync
// anchor at the start isn't enough. Periodic re-anchoring bounds that drift
// to whatever accumulates within one interval, using the same multi-segment
// mechanism already applied on speed changes and seeks.
const RESYNC_INTERVAL_MS = 5000;

function useEvent(handler) {
  const ref = useRef(handler);
  ref.current = handler;
  return useCallback((...args) => ref.current(...args), []);
}


function useOperationGate() {
  const operationRef = useRef(Symbol());
  const waiters = useRef(new Set());
  const beginOperation = useCallback(() => {
    operationRef.current = Symbol();
    waiters.current.forEach((resolve) => resolve(null));
    waiters.current.clear();
    return operationRef.current;
  }, []);
  const waitForOperation = async (pending, operation) => {
    if (operation !== operationRef.current) return null;
    let cancel;
    const superseded = new Promise((resolve) => {
      cancel = resolve;
      waiters.current.add(resolve);
    });
    try {
      return await Promise.race([pending, superseded]);
    } finally {
      waiters.current.delete(cancel);
    }
  };
  return { operationRef, beginOperation, waitForOperation };
}

async function playMaster(media) {
  let timer;
  try {
    await Promise.race([
      Promise.resolve().then(() => media.play()),
      new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error("Master media playback timed out")), 4000);
      })
    ]);
  } catch (error) {
    if (media.paused !== false) throw error;
  } finally {
    clearTimeout(timer);
  }
}

export default function useKaraokeTransport({
  song,
  onlineRoom,
  navigate,
  instrumentalRef,
  vocalsRef,
  videoRef,
  durationRef,
  currentTime,
  duration,
  isPlaying,
  musicVolume,
  speed,
  vocalVolume,
  microphoneVolume,
  microphoneEffects,
  startMelodyGuide,
  silenceMelodyGuide,
  syncSecondaryMedia,
  setCurrentTime,
  playback,
  releaseMonitoring
}) {
  const [analysisRecordingId, setAnalysisState] = useState(null);
  const [recordingError, setRecordingError] = useState(null);
  const analysisRecordingIdRef = useRef(null);
  const roomCaptureRef = useRef(null);
  const stopVersionRef = useRef(0);
  const stopInFlightRef = useRef(null);
  const { operationRef, beginOperation, waitForOperation } = useOperationGate();

  const setAnalysisRecordingId = useCallback((value) => {
    setAnalysisState((previous) => {
      const next = typeof value === "function" ? value(previous) : value;
      analysisRecordingIdRef.current = next;
      return next;
    });
  }, []);

  const {
    recordingSessionId,
    sessionRef,
    pendingRecordingStartRef,
    clearSession,
    discardSession,
    runRecording,
    pauseRecording,
    syncRecording,
    flushRecording
  } = useKaraokeRecording({
    song,
    onlineRoom,
    instrumentalRef,
    musicVolume,
    speed,
    microphoneVolume,
    microphoneEffects,
    setRecordingError,
    setAnalysisRecordingId,
    operationRef,
    beginOperation,
    roomCaptureRef
  });

  useEffect(() => {
    if (!isPlaying) return undefined;
    const interval = setInterval(() => {
      const id = sessionRef.current;
      const instrumental = instrumentalRef.current;
      if (!id || !instrumental) return;
      syncRecording(id, instrumental.currentTime, speed).catch(() => {});
    }, RESYNC_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [instrumentalRef, isPlaying, sessionRef, speed, syncRecording]);

  const pauseMedia = () => {
    [instrumentalRef.current, vocalsRef.current, videoRef.current].forEach((media) => media?.pause());
    silenceMelodyGuide();
  };

  const startRoomCapture = async () => {
    if (!onlineRoom?.room) return null;
    if (roomCaptureRef.current) {
      await safe(() => roomCaptureRef.current.resume?.());
      return roomCaptureRef.current;
    }
    roomCaptureRef.current = await safe(
      () =>
        createRoomVoiceCapture(
          onlineRoom.getRemoteVoiceStreams?.() || [],
          () => instrumentalRef.current?.currentTime ?? 0
        ),
      null
    );
    return roomCaptureRef.current;
  };

  const stopRoomCapture = () => {
    const capture = roomCaptureRef.current;
    roomCaptureRef.current = null;
    if (!capture) return Promise.resolve(null);
    return Promise.resolve()
      .then(() => capture.stop())
      .then((blob) => ({ blob, startPlaybackSec: capture.startPlaybackSec }));
  };

  const togglePlay = async ({ broadcast: shouldBroadcast = true, forcePlaying = null } = {}) => {
    const instrumental = instrumentalRef.current;
    if (!instrumental || !song?.id) return false;

    const shouldPlay = forcePlaying ?? !isPlaying;
    const operation = beginOperation();
    const stopVersion = stopVersionRef.current;
    const stale = () => operation !== operationRef.current;

    if (!shouldPlay) {
      playback.pause();
      const pending = pendingRecordingStartRef.current;
      if (!sessionRef.current && pending?.songId === song.id) pending.settle = "pause";
      pauseMedia();
      await safe(() => roomCaptureRef.current?.pause?.());
      setCurrentTime(instrumental.currentTime);
      playback.paused();
      if (shouldBroadcast) broadcast("pause", instrumental.currentTime);
      if (sessionRef.current) await pauseRecording(sessionRef.current).catch(() => {});
      return true;
    }

    playback.start();
    const scheduledAt =
      shouldBroadcast && onlineRoom?.room && typeof onlineRoom.roomClockNow === "function"
        ? onlineRoom.roomClockNow() + 450
        : null;
    if (scheduledAt != null) broadcast("play", instrumental.currentTime, scheduledAt);

    safe(startMelodyGuide);
    syncSecondaryMedia(instrumental.currentTime, true);
    instrumental.volume = playbackGain(musicVolume);
    if (vocalsRef.current) vocalsRef.current.volume = playbackGain(vocalVolume);

    const recordingStart = runRecording(operation);
    try {
      // Opening the recording session (a network round trip plus a backend
      // microphone/device open) and counting down to the broadcast schedule
      // used to happen one after the other -- a slow recording start (mic
      // device open taking a few hundred ms is routine) delayed the schedule
      // countdown by exactly that much, so the host could start noticeably
      // later than every other participant, who only wait for the schedule
      // itself. Run both concurrently instead; recording still gates
      // playback (a failure here still aborts below), it just no longer
      // gates the countdown too.
      await Promise.all([
        waitForOperation(recordingStart, operation),
        scheduledAt != null ? wait(scheduledAt - onlineRoom.roomClockNow()) : Promise.resolve()
      ]);
      if (stale()) {
        pauseMedia();
        return false;
      }

      await startRoomCapture();
      if (stale()) {
        pauseMedia();
        await stopRoomCapture().catch(() => null);
        return false;
      }

      for (const media of [vocalsRef.current, videoRef.current]) {
        if (media) safe(() => media.play());
      }
      await playMaster(instrumental);

      if (stale()) {
        pauseMedia();
        await safe(() => roomCaptureRef.current?.pause?.());
        return stopVersionRef.current === stopVersion;
      }

      syncSecondaryMedia(instrumental.currentTime, true);
      recordingStart.then((id) => {
        if (!id || stale()) return;
        syncRecording(id, instrumental.currentTime, speed).catch((error) => {
          if (!stale()) {
            setRecordingError(formatError("karaoke.couldNotPreciselySynchronizeRecording", error));
          }
        });
      });
    } catch {
      beginOperation();
      const pending = pendingRecordingStartRef.current;
      if (pending?.songId === song.id) pending.settle = "stop";
      pauseMedia();
      await stopRoomCapture().catch(() => null);
      if (sessionRef.current) await discardSession(sessionRef.current);
      playback.fail();
      setRecordingError(t("karaoke.failedToStartPlayback"));
      if (scheduledAt != null) broadcast("pause", instrumental.currentTime);
      return false;
    }

    playback.played();
    if (shouldBroadcast && scheduledAt == null) broadcast("play", instrumental.currentTime);
    return true;
  };

  const runStop = async (shouldBroadcast) => {
    const instrumental = instrumentalRef.current;
    if (!instrumental || !song?.id) return false;

    playback.stop();
    stopVersionRef.current += 1;
    beginOperation();
    const pending = pendingRecordingStartRef.current;
    if (!sessionRef.current && pending?.songId === song.id) pending.settle = "stop";

    pauseMedia();
    const roomAudio = stopRoomCapture().catch(() => null);
    instrumental.currentTime = 0;
    syncSecondaryMedia(0, true);
    setCurrentTime(0);
    playback.stopped();
    if (shouldBroadcast) broadcast("stop", 0);

    const id = sessionRef.current;
    if (id) {
      await flushRecording();
      const { recording, error } = await finalizeRecording(id);
      if (error) {
        setRecordingError(formatError("karaoke.failedToSaveEntry", error));
        clearSession(id, false);
      } else {
        if (recording?.id) {
          const captured = await roomAudio;
          if (captured?.blob?.size) {
            try {
              const latency = await safe(() => onlineRoom?.estimateRemoteVoiceLatency?.(), 0);
              await api.attachRoomAudio(recording.id, captured.blob, captured.startPlaybackSec, latency);
            } catch (error) {
              setRecordingError(
                formatError("karaoke.recordingSavedButTheOtherParticipantSVoiceCould", error)
              );
            }
          }
          setAnalysisRecordingId(recording.id);
        }
        clearSession(id);
      }
    }

    try {
      await releaseMonitoring?.();
    } catch (error) {
      setRecordingError(formatError("karaoke.couldNotDisableMicrophoneMonitoring", error));
    }
    return true;
  };

  const stop = ({ broadcast: shouldBroadcast = true } = {}) => {
    if (stopInFlightRef.current) return stopInFlightRef.current;
    const request = runStop(shouldBroadcast).finally(() => {
      if (stopInFlightRef.current === request) stopInFlightRef.current = null;
    });
    stopInFlightRef.current = request;
    return request;
  };

  const seekTo = (time, { broadcast: shouldBroadcast = true } = {}) => {
    const instrumental = instrumentalRef.current;
    if (!instrumental || !song?.id) return;
    const position = clampPlaybackPosition(time, durationRef.current);
    instrumental.currentTime = position;
    syncSecondaryMedia(position, true);
    setCurrentTime(position);
    if (isPlaying && sessionRef.current) syncRecording(sessionRef.current, position, speed).catch(() => {});
    if (shouldBroadcast) broadcast("seek", position);
  };

  const skip = (delta) =>
    seekTo(clampPlaybackPosition((Number(instrumentalRef.current?.currentTime) || currentTime) + delta, duration));

  const returnToLibrary = async ({ alreadyStopped = false, analysisId = null } = {}) => {
    if (onlineRoom?.room) safe(() => onlineRoom.syncCommand?.({ type: "open-library" }));
    if (!alreadyStopped) {
      try {
        await stop({ broadcast: false });
      } catch (error) {
        setRecordingError(formatError("karaoke.failedToSaveEntry", error));
      }
    }
    navigate(
      "/",
      alreadyStopped
        ? { replace: true, state: { fromKaraokeFade: true, analysisRecordingId: analysisId || null } }
        : undefined
    );
    return true;
  };

  const broadcast = useKaraokeRoomTransport({
    onlineRoom,
    songId: song?.id,
    instrumentalRef,
    isPlaying,
    navigate,
    seekTo,
    speed,
    stop,
    togglePlay,
    setRecordingError
  });

  const stableReturnToLibrary = useEvent(returnToLibrary);
  const stableSeekTo = useEvent(seekTo);
  const stableSkip = useEvent(skip);
  const stableStop = useEvent(stop);
  const stableTogglePlay = useEvent(togglePlay);
  const clearAnalysis = useCallback(() => setAnalysisRecordingId(null), [setAnalysisRecordingId]);

  return {
    returnToLibrary: stableReturnToLibrary,
    seekTo: stableSeekTo,
    skip: stableSkip,
    stop: stableStop,
    togglePlay: stableTogglePlay,
    recordingSessionId,
    recordingError,
    analysisRecordingId,
    analysisRecordingIdRef,
    clearAnalysis
  };
}
