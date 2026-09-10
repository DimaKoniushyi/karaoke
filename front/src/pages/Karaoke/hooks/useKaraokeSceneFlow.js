import { useCallback, useEffect, useRef, useState } from "react";
import useLatestRef from "../../../hooks/useLatestRef";
import useMountedRef from "../../../hooks/useMountedRef";
import { setGlobalRouteBlackout } from "../../../utils/route-blackout";
import { safe } from "../utils/async";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const step = async (ms, active, action) => {
  await wait(ms);
  if (!active()) return false;
  action?.();
  return true;
};

const waitForMedia = (media) => {
  if (!media || media.readyState >= 3) return Promise.resolve();
  media.load?.();

  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      media.removeEventListener("canplay", done);
      media.removeEventListener("error", done);
      resolve();
    };
    const timer = setTimeout(done, 2200);
    media.addEventListener("canplay", done, { once: true });
    media.addEventListener("error", done, { once: true });
  });
};

export default function useKaraokeSceneFlow({
  analysisRecordingIdRef,
  autoStartRequested,
  roomPrepared,
  hideControls,
  instrumentalRef,
  isPlaying,
  isRadioPlaying,
  navigate,
  returnToLibrary,
  setRecordingActive,
  showControls,
  songId,
  stop,
  togglePlay,
  turnOffRadio,
  turnOnRadio,
  vocalsRef
}) {
  const mounted = useMountedRef();
  const transition = useRef(0);
  const transitioning = useRef(false);
  const resumeRadio = useRef(false);
  const started = useRef(false);
  const stageTimer = useRef();
  const autoStarted = useRef();
  const autoStarting = useRef();
  const roomRevealed = useRef();
  const initialTransition = autoStartRequested || roomPrepared;
  const [stageActionsVisible, setStageActionsVisible] = useState(true);
  const [sceneBlackout, setSceneBlackout] = useState(initialTransition);
  const [sceneIntroVisible, setSceneIntroVisible] = useState(false);
  const [sceneTransitioning, setSceneTransitioning] = useState(initialTransition);

  const current = useCallback(
    (id) => mounted.current && transition.current === id,
    [mounted]
  );

  const revealStageActions = useCallback(() => {
    clearTimeout(stageTimer.current);
    setStageActionsVisible(true);
    stageTimer.current = setTimeout(() => {
      if (mounted.current) setStageActionsVisible(false);
    }, 1800);
  }, [mounted]);

  const restoreScene = useCallback(
    (autoHide = true) => {
      transitioning.current = false;
      setSceneTransitioning(false);
      setSceneIntroVisible(false);
      setSceneBlackout(false);
      showControls();
      if (autoHide) revealStageActions();
      else setStageActionsVisible(true);
    },
    [revealStageActions, showControls]
  );

  const beginTransition = useCallback(() => {
    if (transitioning.current) return null;
    transitioning.current = true;
    const id = ++transition.current;
    setSceneTransitioning(true);
    hideControls();
    setStageActionsVisible(false);
    setSceneIntroVisible(false);
    setSceneBlackout(true);
    return () => current(id);
  }, [current, hideControls]);

  useEffect(() => {
    transition.current += 1;
    transitioning.current = false;
    started.current = false;
    autoStarted.current = null;
    autoStarting.current = null;
    roomRevealed.current = null;
    setSceneIntroVisible(false);
    setSceneBlackout(initialTransition);
    setSceneTransitioning(initialTransition);
  }, [autoStartRequested, roomPrepared, songId]);

  useEffect(() => {
    if (!initialTransition) return;
    const timer = setTimeout(() => setGlobalRouteBlackout(false), 80);
    return () => clearTimeout(timer);
  }, [initialTransition]);

  useEffect(() => {
    revealStageActions();
    return () => clearTimeout(stageTimer.current);
  }, [revealStageActions]);

  const runIntroTransition = useCallback(
    async (action) => {
      const active = beginTransition();
      if (!active) return false;
      const preload = safe(
        () =>
          Promise.all(
            [instrumentalRef.current, vocalsRef.current].filter(Boolean).map(waitForMedia)
          ),
        false
      );

      try {
        if (!(await step(420, active, () => setSceneIntroVisible(true)))) return false;
        if (!(await step(1350, active, () => setSceneIntroVisible(false)))) return false;
        if (!(await step(180, active))) return false;
        await preload;
        if (!active()) return false;
        setSceneBlackout(false);
        if (!(await step(520, active))) return false;
        restoreScene();
        return Boolean(await action());
      } catch {
        return false;
      } finally {
        if (active()) {
          setSceneBlackout(false);
          await wait(120);
          if (active()) restoreScene();
        }
      }
    },
    [beginTransition, instrumentalRef, restoreScene, vocalsRef]
  );

  const startSong = useCallback(async () => {
    resumeRadio.current = isRadioPlaying;
    turnOffRadio({ remember: false });
    const success = await runIntroTransition(() => togglePlay({ forcePlaying: true }));
    if (success) started.current = true;
    else if (resumeRadio.current && mounted.current) {
      safe(() => turnOnRadio({ remember: false, fadeIn: true }), false);
    }
    return success;
  }, [isRadioPlaying, mounted, runIntroTransition, togglePlay, turnOffRadio, turnOnRadio]);
  const startSongRef = useLatestRef(startSong);

  const handleTogglePlay = useCallback(async () => {
    if (!isPlaying) {
      if (!started.current) return startSong();
      turnOffRadio({ remember: false });
      return togglePlay({ forcePlaying: true });
    }

    const paused = await togglePlay({ forcePlaying: false });
    if (paused && resumeRadio.current) {
      setRecordingActive(false);
      safe(() => turnOnRadio({ remember: false, fadeIn: true }), false);
    }
    return paused;
  }, [isPlaying, setRecordingActive, startSong, togglePlay, turnOffRadio, turnOnRadio]);

  const navigateFromBlackout = useCallback(
    (analysisId = null) =>
      navigate("/", {
        replace: true,
        state: { fromKaraokeFade: true, analysisRecordingId: analysisId || null }
      }),
    [navigate]
  );

  const handleStop = useCallback(async () => {
    const active = beginTransition();
    if (!active) return false;

    try {
      if (!(await step(430, active))) return false;
      if (!(await stop()) || !active()) {
        if (active()) restoreScene(false);
        return false;
      }

      started.current = false;
      const analysisId = analysisRecordingIdRef.current;
      setGlobalRouteBlackout(true);
      if (!(await step(40, active))) return false;

      if (returnToLibrary) await returnToLibrary({ alreadyStopped: true, analysisId });
      else navigateFromBlackout(analysisId);
      return true;
    } catch {
      if (active()) {
        restoreScene(false);
        setGlobalRouteBlackout(false);
      }
      return false;
    }
  }, [analysisRecordingIdRef, beginTransition, navigateFromBlackout, restoreScene, returnToLibrary, stop]);

  useEffect(() => {
    if (!autoStartRequested || !songId || autoStarted.current === songId) return;

    let cancelled = false;
    let failures = 0;
    let timer;
    const deadline = Date.now() + 30_000;
    const fail = () => {
      setSceneBlackout(false);
      setSceneTransitioning(false);
      transitioning.current = false;
      showControls();
    };
    const schedule = (delay = 120) => {
      timer = setTimeout(run, delay);
    };
    const run = () => {
      if (cancelled) return;
      if (instrumentalRef.current?.readyState < 3) {
        if (Date.now() < deadline) schedule();
        else fail();
        return;
      }
      if (autoStarting.current === songId) return;

      autoStarting.current = songId;
      safe(() => startSongRef.current(), false).then((success) => {
        if (autoStarting.current === songId) autoStarting.current = null;
        if (cancelled) return;
        if (success) autoStarted.current = songId;
        else if (++failures <= 2) schedule(600);
        else fail();
      });
    };

    schedule(80);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [autoStartRequested, instrumentalRef, showControls, songId, startSongRef]);

  useEffect(() => {
    if (autoStartRequested || !roomPrepared || !songId || roomRevealed.current === songId) return;
    let cancelled = false;
    safe(() => runIntroTransition(() => !cancelled), false).then((shown) => {
      if (!cancelled && shown) roomRevealed.current = songId;
    });
    return () => {
      cancelled = true;
    };
  }, [autoStartRequested, roomPrepared, runIntroTransition, songId]);

  useEffect(
    () => () => {
      transition.current += 1;
      transitioning.current = false;
      clearTimeout(stageTimer.current);
    },
    []
  );

  return {
    handleStop,
    handleTogglePlay,
    navigateToLibraryFromBlackout: navigateFromBlackout,
    revealStageActions,
    sceneBlackout,
    sceneIntroVisible,
    sceneTransitioning,
    stageActionsVisible
  };
}
