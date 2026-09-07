import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import useMountedRef from "../../../hooks/useMountedRef";
import { translateSaved as t } from "../../../i18n/runtime";
import { playbackGain } from "../utils/data";
import {
  finalizeRecording,
  forgetPending,
  formatError,
  pendingRecordingIds,
  rememberPending
} from "../utils/recordingSession";

const MISSING_ID = "karaoke.backendDidNotReturnPostId";
const noop = () => {};
const now = () => globalThis.performance?.now?.() ?? Date.now();

function useQueue() {
  const queue = useRef(Promise.resolve());
  const run = useCallback((task) => {
    const next = queue.current.then(task, task);
    queue.current = next.catch(noop);
    return next;
  }, []);
  const flush = useCallback(() => queue.current.catch(noop), []);
  return [run, flush];
}

// Like useQueue, but a task queued while an earlier one is still waiting its
// turn supersedes it instead of running behind it -- only the most recently
// queued state as of when the chain actually gets to it is ever sent. Used
// for control-slider updates (volume/reverb/echo/delay/octave): dragging a
// slider from 50 to 90 used to send every intermediate value in order, even
// though only 90 is ever needed, and Stop had to wait out that whole queue.
function useCoalescingQueue() {
  const queue = useRef(Promise.resolve());
  const latestToken = useRef(0);
  const run = useCallback((task) => {
    const token = ++latestToken.current;
    const runIfCurrent = () => (token === latestToken.current ? task() : undefined);
    const next = queue.current.then(runIfCurrent, runIfCurrent);
    queue.current = next.catch(noop);
    return next;
  }, []);
  const flush = useCallback(() => queue.current.catch(noop), []);
  return [run, flush];
}

export default function useKaraokeRecording({
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
}) {
  const mounted = useMountedRef();
  const [recordingSessionId, setRecordingSessionId] = useState(null);
  const sessionRef = useRef(null);
  const pendingStartRef = useRef(null);
  const previousSpeed = useRef(speed);
  const [queueRequest, flushRequests] = useQueue();
  const [queueControls, flushControls] = useCoalescingQueue();

  const setSession = useCallback(
    (id) => {
      sessionRef.current = id;
      if (mounted.current) setRecordingSessionId(id);
    },
    [mounted]
  );

  // position is read on the frontend before this request is even queued, let
  // alone before the backend anchors the recording to it -- by the time
  // sync_playback() actually runs, the instrumental has moved on by however
  // long that round trip took. Pre-compensate using the previous call's own
  // measured round trip (halved, as a one-way estimate) so each anchor lands
  // closer to the position the backend will see at the moment it processes it.
  const lastLatencySecRef = useRef(0);
  const syncRecording = useCallback(
    (id, position, rate) => {
      const compensated = position + lastLatencySecRef.current * (Number(rate) || 1);
      // Measured from just before the actual network call, not from before
      // queueRequest -- an earlier queued request still in flight ahead of
      // this one used to count as part of this call's "latency" too, wildly
      // overestimating it (JS queue wait time, not RTT) and pushing the next
      // sync's compensation off by however long that wait was.
      return queueRequest(() => {
        const startedAt = now();
        return api.syncRecording(id, compensated, rate).then((result) => {
          lastLatencySecRef.current = Math.max(0, (now() - startedAt) / 2 / 1000);
          return result;
        });
      });
    },
    [queueRequest]
  );
  const flushRecording = useCallback(
    () => Promise.all([flushRequests(), flushControls()]),
    [flushControls, flushRequests]
  );

  const clearSession = useCallback(
    (id, forget = true) => {
      if (sessionRef.current !== id) return;
      if (forget) forgetPending(id);
      setSession(null);
    },
    [setSession]
  );

  const discardSession = useCallback(
    async (id) => {
      if (!id) return;
      await flushRecording();
      const { error } = await finalizeRecording(id);
      if (!error) clearSession(id);
    },
    [clearSession, flushRecording]
  );

  useEffect(() => {
    const previous = previousSpeed.current;
    previousSpeed.current = speed;
    const audio = instrumentalRef.current;
    if (previous !== speed && recordingSessionId && audio) {
      syncRecording(recordingSessionId, audio.currentTime, speed).catch(noop);
    }
  }, [instrumentalRef, recordingSessionId, speed, syncRecording]);

  useEffect(() => {
    if (!recordingSessionId) return;
    let active = true;

    queueControls(() =>
      api.updateRecordingControls(recordingSessionId, {
        musicVolume: playbackGain(musicVolume),
        microphoneVolume,
        reverb: microphoneEffects.reverb,
        echo: microphoneEffects.echo,
        delay: microphoneEffects.delay,
        octave: microphoneEffects.octave ?? 0
      })
    ).catch((error) => {
      if (active && sessionRef.current === recordingSessionId) {
        setRecordingError(formatError("settings.couldNotSaveAudioSettings", error));
      }
    });

    return () => {
      active = false;
    };
  }, [
    microphoneEffects.delay,
    microphoneEffects.echo,
    microphoneEffects.octave,
    microphoneEffects.reverb,
    microphoneVolume,
    musicVolume,
    queueControls,
    recordingSessionId,
    setRecordingError
  ]);

  useEffect(() => {
    let active = true;
    beginOperation();

    pendingRecordingIds().forEach((id) => {
      finalizeRecording(id)
        .then(({ recording }) => {
          if (active && mounted.current && recording?.id) setAnalysisRecordingId(recording.id);
        })
        .catch(noop);
    });

    return () => {
      active = false;
      beginOperation();
      const capture = roomCaptureRef.current;
      roomCaptureRef.current = null;
      Promise.resolve().then(() => capture?.stop?.()).catch(noop);

      const pending = pendingStartRef.current;
      if (pending && pending.songId === song?.id) pending.settle = "stop";

      const id = sessionRef.current;
      setSession(null);
      if (!id) return;
      flushRecording().then(() => finalizeRecording(id)).catch(noop);
    };
  }, [
    beginOperation,
    flushRecording,
    mounted,
    roomCaptureRef,
    setAnalysisRecordingId,
    setSession,
    song?.id
  ]);

  const startRecording = async () => {
    // A previous take's measured RTT must never carry into a brand new
    // recording session's first sync -- there has been no measurement for
    // this one yet.
    lastLatencySecRef.current = 0;
    const { recording_session_id: id } =
      (await api.startRecording(
        song.id,
        instrumentalRef.current.currentTime,
        playbackGain(musicVolume),
        microphoneVolume,
        microphoneEffects.reverb,
        microphoneEffects.echo,
        microphoneEffects.delay,
        Boolean(onlineRoom?.room),
        microphoneEffects.octave,
        speed,
        Boolean(onlineRoom?.voiceRef?.current?.usingRelay)
      )) || {};

    if (!id) throw new Error(t(MISSING_ID));
    rememberPending(id);
    return id;
  };

  const getPendingStart = (operation) => {
    const current = pendingStartRef.current;
    if (current?.songId === song.id) {
      current.latestOperation = operation;
      current.settle = null;
      return current;
    }

    const pending = {
      songId: song.id,
      latestOperation: operation,
      settle: null
    };
    pending.promise = startRecording().finally(() => {
      if (pendingStartRef.current === pending) pendingStartRef.current = null;
    });
    pendingStartRef.current = pending;
    return pending;
  };

  const runRecording = async (operation) => {
    let id = sessionRef.current;
    let pending;

    try {
      if (id) {
        rememberPending(id);
        await queueRequest(() => api.resumeRecording(id));
      } else {
        pending = getPendingStart(operation);
        id = await pending.promise;
      }

      if (operation !== operationRef.current) {
        if (pending?.latestOperation !== operation && pending) return null;
        if (pending?.settle === "pause") {
          await queueRequest(() => api.pauseRecording(id)).catch(noop);
          setSession(id);
          return null;
        }
        await discardSession(id);
        if (pending?.settle === "stop") setSession(null);
        return null;
      }

      setSession(id);
      if (mounted.current) setRecordingError(null);
      return id;
    } catch (error) {
      if (operation !== operationRef.current) return null;
      if (id) await discardSession(id);
      if (mounted.current) {
        setRecordingError(
          formatError("karaoke.recordingIsNotAvailableKaraokeWillContinueToWork", error)
        );
      }
      return null;
    }
  };

  return {
    recordingSessionId,
    sessionRef,
    pendingRecordingStartRef: pendingStartRef,
    clearSession,
    discardSession,
    runRecording,
    pauseRecording: (id) => queueRequest(() => api.pauseRecording(id)),
    syncRecording,
    flushRecording
  };
}
