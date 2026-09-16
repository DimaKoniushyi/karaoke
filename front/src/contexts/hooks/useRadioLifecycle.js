import { useEffect } from "react";
import { api } from "../../api/client";
import { closeAudioContextQuietly } from "../../utils/audio-context";

export default function useRadioLifecycle({
  analyserRef,
  audioContextRef,
  audioRef,
  cancelVolumeFade,
  enabled,
  frequencyDataRef,
  pendingStartupPlaybackRef,
  playbackVersionRef,
  suspendedRef,
  turnOnRef,
  unlockAudioAnalysis,
  createVersion,
  stopAnalysis
}) {
  useEffect(() => {
    const audio = audioRef.current;
    if (enabled) turnOnRef.current({ remember: false, analyse: true, fadeIn: true });
    return () => {
      playbackVersionRef.current = createVersion();
      cancelVolumeFade();
      stopAnalysis();
      audio?.pause();
      api.setDirectMonitorMediaActive(false).catch(() => {});
      audio?.removeAttribute("src");
      audio?.load();
      const context = audioContextRef.current;
      audioContextRef.current = null;
      analyserRef.current = null;
      frequencyDataRef.current = null;
      closeAudioContextQuietly(context);
    };
  }, [
    analyserRef,
    audioContextRef,
    audioRef,
    cancelVolumeFade,
    createVersion,
    enabled,
    frequencyDataRef,
    playbackVersionRef,
    stopAnalysis,
    turnOnRef
  ]);

  useEffect(() => {
    const unlock = () => {
      if (pendingStartupPlaybackRef.current && enabled && !suspendedRef.current) {
        pendingStartupPlaybackRef.current = false;
        turnOnRef.current({ remember: false, analyse: true, fadeIn: true });
      } else unlockAudioAnalysis();
    };
    window.addEventListener("pointerdown", unlock, true);
    window.addEventListener("keydown", unlock, true);
    return () => {
      window.removeEventListener("pointerdown", unlock, true);
      window.removeEventListener("keydown", unlock, true);
    };
  }, [enabled, pendingStartupPlaybackRef, suspendedRef, turnOnRef, unlockAudioAnalysis]);
}
