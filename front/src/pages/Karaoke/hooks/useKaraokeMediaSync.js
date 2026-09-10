import { useCallback, useEffect, useRef } from "react";
import { safe } from "../utils/async";
import { playbackGain } from "../utils/data";
import {
  classifyDrift,
  driftCorrectedRate,
  getSecondaryMediaPosition,
  normalizePlaybackRate
} from "../utils/transport";

const now = () => globalThis.performance?.now?.() ?? Date.now();

export default function useKaraokeMediaSync({
  currentTimeRef,
  instrumentalRef,
  isPlaying,
  keyShift,
  melodyVolume,
  musicVolume,
  onPlaybackEndedRef,
  setCurrentTime,
  setDuration,
  setIsPlaying,
  silenceMelodyGuide,
  songId,
  speed,
  startMelodyGuide,
  updateMelodyGuide,
  videoRef,
  vocalVolume,
  vocalsRef
}) {
  const syncAt = useRef({ react: 0, secondary: 0 });

  const syncSecondaryMedia = useCallback(
    (position, force = false) => {
      const rate = normalizePlaybackRate(speed);

      for (const media of [vocalsRef.current, videoRef.current]) {
        if (!media || !Number.isFinite(media.duration) || media.duration <= 0) continue;

        const target = getSecondaryMediaPosition(position, media.duration);
        const drift = media.currentTime - target;
        const correction = force ? "hard" : classifyDrift(drift);

        try {
          if (correction === "hard") media.currentTime = target;
          media.playbackRate = ["soft", "strong"].includes(correction)
            ? driftCorrectedRate(rate, drift, correction)
            : rate;
        } catch {
          // Media can disappear while its source is being replaced.
        }
      }
    },
    [speed, videoRef, vocalsRef]
  );

  useEffect(() => {
    const audio = instrumentalRef.current;
    if (!audio) return;

    const syncDuration = () => {
      const value = Number(audio.duration);
      setDuration(Number.isFinite(value) ? Math.max(0, value) : 0);
    };
    const syncTime = () => {
      const value = Number(audio.currentTime);
      if (!Number.isFinite(value) || value < 0) return;
      currentTimeRef.current = value;
      setCurrentTime(value);
    };
    const ended = () => {
      if (onPlaybackEndedRef.current) return safe(onPlaybackEndedRef.current);
      vocalsRef.current?.pause();
      videoRef.current?.pause();
      silenceMelodyGuide();
      setIsPlaying(false);
    };

    audio.addEventListener("loadedmetadata", syncDuration);
    audio.addEventListener("durationchange", syncDuration);
    audio.addEventListener("timeupdate", syncTime);
    audio.addEventListener("ended", ended);
    syncDuration();

    return () => {
      audio.removeEventListener("loadedmetadata", syncDuration);
      audio.removeEventListener("durationchange", syncDuration);
      audio.removeEventListener("timeupdate", syncTime);
      audio.removeEventListener("ended", ended);
    };
  }, [
    currentTimeRef,
    instrumentalRef,
    onPlaybackEndedRef,
    setCurrentTime,
    setDuration,
    setIsPlaying,
    silenceMelodyGuide,
    songId,
    videoRef,
    vocalsRef
  ]);

  useEffect(() => {
    if (!isPlaying || !globalThis.requestAnimationFrame) return;

    let frame;
    const update = () => {
      const position = Number(instrumentalRef.current?.currentTime);

      if (Number.isFinite(position) && position >= 0) {
        currentTimeRef.current = position;
        updateMelodyGuide(position);
        const time = now();

        if (time - syncAt.current.react >= 100) {
          setCurrentTime(position);
          syncAt.current.react = time;
        }
        if (time - syncAt.current.secondary >= 450) {
          syncSecondaryMedia(position);
          syncAt.current.secondary = time;
        }
      }

      frame = requestAnimationFrame(update);
    };

    const resync = () => {
      if (globalThis.document?.visibilityState !== "visible") return;
      const position = Number(instrumentalRef.current?.currentTime);
      if (!Number.isFinite(position) || position < 0) return;

      currentTimeRef.current = position;
      setCurrentTime(position);
      syncAt.current.react = syncAt.current.secondary = now();
      syncSecondaryMedia(position, true);
    };

    globalThis.document?.addEventListener?.("visibilitychange", resync);
    frame = requestAnimationFrame(update);

    return () => {
      cancelAnimationFrame(frame);
      globalThis.document?.removeEventListener?.("visibilitychange", resync);
    };
  }, [currentTimeRef, instrumentalRef, isPlaying, setCurrentTime, syncSecondaryMedia, updateMelodyGuide]);

  useEffect(() => {
    const instrumental = instrumentalRef.current;
    const vocals = vocalsRef.current;
    const rate = normalizePlaybackRate(speed);

    if (instrumental) instrumental.volume = playbackGain(musicVolume);
    if (vocals) vocals.volume = playbackGain(vocalVolume);

    for (const media of [instrumental, vocals, videoRef.current]) {
      if (!media) continue;
      try {
        media.playbackRate = rate;
      } catch {
        // Detached media can reject rate changes.
      }
    }
  }, [instrumentalRef, musicVolume, speed, videoRef, vocalVolume, vocalsRef]);

  useEffect(() => {
    if (isPlaying && melodyVolume > 0) safe(startMelodyGuide);
    else silenceMelodyGuide();
  }, [isPlaying, keyShift, melodyVolume, silenceMelodyGuide, startMelodyGuide]);

  return { syncSecondaryMedia };
}
