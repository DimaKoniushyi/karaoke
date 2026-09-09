import { AlertCircle } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import PerformanceAnalysisModal from "../../components/PerformanceAnalysisModal";
import { useRadio } from "../../contexts/radio";
import useLatestRef from "../../hooks/useLatestRef";
import { Box, Card, Stack, Typography } from "../../theme/ui";
import { isHotkeyScopeActive, shouldIgnoreHotkey } from "../../utils/hotkeys";
import KaraokeStageActions from "./actions";
import KaraokeConsole from "./console";
import useKaraokeSceneFlow from "./hooks/useKaraokeSceneFlow";
import KaraokeMedia from "./media";
import KaraokePerformanceStage from "./performance-stage";

const SEEK_DELTA = { "seek-backward": -5, "seek-forward": 5 };

// Pure key -> command classification, kept separate from useHotkeys so it
// can be unit-tested without mounting a component or touching the DOM.
export function getKaraokeHotkeyAction(event, scope) {
  if (
    event?.code === "Space" &&
    !event.defaultPrevented &&
    !event.isComposing &&
    !event.repeat &&
    isHotkeyScopeActive(scope)
  ) {
    return "toggle-playback";
  }
  if (shouldIgnoreHotkey(event, scope)) return null;
  if (event.code === "Escape") return "stop";
  if (event.code === "ArrowLeft") return "seek-backward";
  if (event.code === "ArrowRight") return "seek-forward";
  return null;
}

export function useHotkeys(scopeRef, { currentTime, duration, onTogglePlay, onSeek, onStop }) {
  const handlers = useLatestRef({ currentTime, duration, onTogglePlay, onSeek, onStop });

  useEffect(() => {
    const onKeyDown = (event) => {
      const action = getKaraokeHotkeyAction(event, scopeRef.current);
      if (action === "toggle-playback") {
        event.preventDefault();
        return handlers.current.onTogglePlay?.();
      }
      if (action === "stop") return handlers.current.onStop?.();
      if (action === "seek-backward" || action === "seek-forward") {
        event.preventDefault();
        const { currentTime: time, duration: length, onSeek: seek } = handlers.current;
        const delta = SEEK_DELTA[action];
        seek?.(
          Math.min(Math.max(0, Number(length) || 0), Math.max(0, (Number(time) || 0) + delta))
        );
      }
    };
    globalThis.addEventListener?.("keydown", onKeyDown);
    return () => globalThis.removeEventListener?.("keydown", onKeyDown);
  }, [handlers, scopeRef]);
}

export function useControls(autoHideEnabled) {
  const [visible, setVisible] = useState(true);
  const activity = useRef(Date.now());
  const pointer = useRef([NaN, NaN]);
  const hidden = useRef(false);

  const showControls = useCallback((manual = false) => {
    if (manual) hidden.current = false;
    if (hidden.current) return;
    activity.current = Date.now();
    setVisible(true);
  }, []);
  const hideControls = useCallback((manual = false) => {
    if (manual) hidden.current = true;
    setVisible(false);
  }, []);
  const revealControls = useCallback((event) => {
    if (hidden.current) return false;
    if (Number.isFinite(event?.clientX) && Number.isFinite(event?.clientY)) {
      const [x, y] = pointer.current;
      if (x === event.clientX && y === event.clientY) return false;
      pointer.current = [event.clientX, event.clientY];
    }
    showControls();
    return true;
  }, [showControls]);

  useEffect(() => {
    if (!autoHideEnabled) return;
    const timer = setInterval(() => !hidden.current && setVisible(Date.now() - activity.current < 2200), 250);
    return () => clearInterval(timer);
  }, [autoHideEnabled]);
  useEffect(() => {
    const reveal = () => showControls(true);
    globalThis.document?.addEventListener?.("fullscreenchange", reveal);
    return () => globalThis.document?.removeEventListener?.("fullscreenchange", reveal);
  }, [showControls]);
  useEffect(() => showControls(true), [autoHideEnabled, showControls]);

  return { controlsVisible: visible, hideControls, revealControls, showControls };
}

export function useStageLayout(ref) {
  useEffect(() => {
    const shell = globalThis.document?.querySelector?.(".karaoke-app-shell");
    const stage = ref.current;
    const main = stage?.parentElement;
    if (!shell || !main || !stage) return;

    const sync = () => {
      const finite = (value) => {
        const number = Number(value);
        return Number.isFinite(number) ? Math.max(0, number) : 0;
      };
      const mainWidth = finite(main.clientWidth);
      const mainHeight = finite(main.clientHeight);
      const stageWidth = finite(stage.clientWidth);
      const stageHeight = finite(stage.clientHeight);
      const nav = finite(parseFloat(globalThis.getComputedStyle(shell).getPropertyValue("--karaoke-nav-extra")));
      shell.style.setProperty("--karaoke-nav-extra", `${Math.max(0, mainHeight + nav - (mainWidth * 9) / 16)}px`);
      stage.style.setProperty("--karaoke-video-width", `${Math.ceil(Math.max(stageWidth, (stageHeight * 16) / 9)) + 2}px`);
      stage.style.setProperty("--karaoke-video-height", `${Math.ceil(Math.max(stageHeight, (stageWidth * 9) / 16)) + 2}px`);
    };
    const observer = globalThis.ResizeObserver ? new globalThis.ResizeObserver(sync) : null;
    observer?.observe(main);
    observer?.observe(stage);
    if (!observer) globalThis.addEventListener?.("resize", sync);
    sync();
    return () => {
      observer?.disconnect();
      globalThis.removeEventListener?.("resize", sync);
      ["--karaoke-nav-extra"].forEach((name) => shell.style.removeProperty(name));
      ["--karaoke-video-width", "--karaoke-video-height"].forEach((name) => stage.style.removeProperty(name));
    };
  }, [ref]);
}


export default function KaraokeView({
  autoHideEnabled,
  currentTime,
  duration,
  isPlaying,
  recordingSessionId,
  recordingError,
  analysisRecordingId,
  clearAnalysis,
  sceneOptions,
  transport,
  mediaProps,
  performanceProps,
  consoleProps
}) {
  const containerRef = useRef(null);
  const [clipAvailable, setClipAvailable] = useState(false);
  const {
    isPlaying: isRadioPlaying,
    setRecordingActive,
    toggle: toggleRadio,
    turnOff: turnOffRadio,
    turnOn: turnOnRadio
  } = useRadio();
  const controls = useControls(autoHideEnabled);
  const { playbackEndedRef, ...flowOptions } = sceneOptions;
  const scene = useKaraokeSceneFlow({
    ...flowOptions,
    hideControls: controls.hideControls,
    isPlaying,
    isRadioPlaying,
    returnToLibrary: transport.returnToLibrary,
    setRecordingActive,
    showControls: controls.showControls,
    stop: transport.stop,
    togglePlay: transport.togglePlay,
    turnOffRadio,
    turnOnRadio
  });

  playbackEndedRef.current = scene.handleStop;

  useEffect(() => {
    setRecordingActive(Boolean(recordingSessionId) && isPlaying);
  }, [isPlaying, recordingSessionId, setRecordingActive]);

  useEffect(() => () => setRecordingActive(false), [setRecordingActive]);

  useHotkeys(containerRef, {
    currentTime,
    duration,
    onTogglePlay: scene.handleTogglePlay,
    onSeek: transport.seekTo,
    onStop: scene.handleStop
  });
  useStageLayout(containerRef);

  const closeAnalysis = () => {
    clearAnalysis();
    scene.navigateToLibraryFromBlackout();
  };
  const controlsVisible = controls.controlsVisible && !scene.sceneTransitioning;

  return (
    <Box
      as="main"
      ref={containerRef}
      data-role="karaoke"
      data-playing={isPlaying || undefined}
      onMouseMove={(event) => {
        if (!scene.sceneTransitioning && controls.revealControls(event)) {
          scene.revealStageActions();
        }
      }}
      sx={{
        position: "fixed",
        inset: 0,
        overflow: "hidden",
        color: "var(--color-text)",
        background: "var(--color-bg-deep)"
      }}
    >
      <KaraokeMedia {...mediaProps} onClipAvailabilityChange={setClipAvailable} />
      <KaraokePerformanceStage
        {...performanceProps}
        hasSongClip={clipAvailable}
        sceneBlackout={scene.sceneBlackout}
        sceneIntroVisible={scene.sceneIntroVisible}
      />
      <KaraokeStageActions
        controlsVisible={controls.controlsVisible}
        hideControls={controls.hideControls}
        isPlaying={isPlaying}
        isRadioPlaying={isRadioPlaying}
        returnToLibrary={transport.returnToLibrary}
        sceneTransitioning={scene.sceneTransitioning}
        showControls={controls.showControls}
        stageActionsVisible={scene.stageActionsVisible}
        toggleRadio={toggleRadio}
      />

      {recordingError && (
        <Stack
          align="center"
          sx={{
            position: "absolute",
            inset: "var(--space-4) var(--space-16) auto",
            zIndex: 30,
            pointerEvents: "none"
          }}
        >
          <Card
            variant="laser"
            tilt={false}
            cardContent={{ style: { padding: "var(--space-3) var(--space-5)" } }}
          >
            <Stack direction="row" align="center" gap="var(--space-2)">
              <AlertCircle aria-hidden />
              <Typography role="alert" tone="danger">
                {recordingError}
              </Typography>
            </Stack>
          </Card>
        </Stack>
      )}

      <KaraokeConsole
        {...consoleProps}
        autoHideEnabled={autoHideEnabled}
        visible={controlsVisible}
        onStop={scene.handleStop}
        onTogglePlay={scene.handleTogglePlay}
      />

      {analysisRecordingId && (
        <PerformanceAnalysisModal
          recordingId={analysisRecordingId}
          onClose={closeAnalysis}
          onDone={closeAnalysis}
          onDeleted={closeAnalysis}
        />
      )}
    </Box>
  );
}
