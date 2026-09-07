import { Pause, Play, Volume2, VolumeX } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import useApiFetchParams from "../hooks/useApiFetchParams";
import { useI18n } from "../i18n";
import { IconButton, Slider, Stack, Typography, Waveform } from "../theme/ui";
import * as platform from "../utils/platform";
import {
  formatAudioTime,
  normalizeAudioDuration,
  normalizeAudioPosition,
  normalizeAudioVolume,
  toggleAudioPlayback
} from "./audio-player-utils";

const setMedia = (media, key, value) => {
  try {
    media[key] = value;
    return true;
  } catch {
    return false;
  }
};

export function AudioPlayer({ src, className = "", initialDuration = 0 }) {
  const { t } = useI18n();
  const media = useRef(null);
  const rememberedVolume = useRef(1);
  const token = platform.apiToken();
  const [playbackSource, setPlaybackSource] = useState(src);
  const waveformFetchParams = useApiFetchParams();
  const fallback = normalizeAudioDuration(initialDuration);
  const [state, setState] = useState({
    duration: fallback,
    playing: false,
    position: 0,
    volume: 1
  });
  const [volumeExpanded, setVolumeExpanded] = useState(false);
  const update = (value) => setState((current) => ({ ...current, ...value }));

  useEffect(() => {
    if (!token || !src || /^(?:blob:|data:)/i.test(src)) {
      setPlaybackSource(src);
      return undefined;
    }
    let active = true;
    let objectUrl = "";
    setPlaybackSource("");
    fetch(src, { headers: { "X-ADVoice-Token": token } })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.blob();
      })
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setPlaybackSource(objectUrl);
      })
      .catch(() => active && setPlaybackSource(src));
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [src, token]);

  useEffect(() => {
    const audio = media.current;
    return () => {
      if (!audio) return;
      audio.pause();
    };
  }, []);

  useEffect(() => {
    media.current?.pause();
    if (media.current) {
      setMedia(media.current, "currentTime", 0);
      try {
        media.current.load();
      } catch {
        // Some test and embedded media implementations do not expose load().
      }
    }
    setState((current) => ({ ...current, duration: fallback, playing: false, position: 0 }));
  }, [fallback, playbackSource]);

  useEffect(() => {
    if (!state.playing) return undefined;
    let frame = 0;
    let lastSync = 0;
    const draw = (now) => {
      const audio = media.current;
      if (!audio || audio.paused) return;
      // The progress bar looks identical to a viewer whether it updates 60
      // times a second or 10, so the position state is synced at ~100ms
      // instead of every animation frame to avoid re-rendering the player
      // (and its Waveform/Slider children) 60 times a second.
      if (now - lastSync >= 100) {
        lastSync = now;
        setState((current) => ({
          ...current,
          position: normalizeAudioPosition(audio.currentTime, state.duration)
        }));
      }
      frame = requestAnimationFrame(draw);
    };
    frame = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frame);
  }, [state.duration, state.playing]);

  const seek = (value) => {
    const position = normalizeAudioPosition(value, state.duration);
    if (setMedia(media.current, "currentTime", position)) update({ position });
  };
  const setVolume = (value) => {
    const volume = normalizeAudioVolume(value);
    if (volume) rememberedVolume.current = volume;
    if (setMedia(media.current, "volume", volume)) update({ volume });
  };

  return (
    <Stack
      className={["performance-player", className].filter(Boolean).join(" ")}
      direction="row"
      align="center"
      gap="var(--space-2)"
      sx={{ flex: "1 1 auto", minInlineSize: 0 }}
    >
      <audio
        ref={media}
        preload="metadata"
        src={playbackSource || undefined}
        onLoadedMetadata={(event) =>
          update({ duration: normalizeAudioDuration(event.currentTarget.duration) || fallback })
        }
        onTimeUpdate={(event) =>
          update({
            position: normalizeAudioPosition(
              event.currentTarget.currentTime,
              normalizeAudioDuration(event.currentTarget.duration) || state.duration
            )
          })
        }
        onPlay={() => update({ playing: true })}
        onPause={() => update({ playing: false })}
        onEnded={(event) => {
          setMedia(event.currentTarget, "currentTime", 0);
          update({ playing: false, position: 0 });
        }}
      />
      <IconButton
        icon={state.playing ? Pause : Play}
        label={t(state.playing ? "audio.pause" : "audio.playRecording")}
        onClick={() => toggleAudioPlayback(media.current)}
      />
      <Stack
        className="performance-player-timeline"
        sx={{ alignSelf: "stretch", flex: "1 1 0", minInlineSize: 0, width: "auto" }}
      >
        <Waveform
          label={t("audio.recordingPosition")}
          value={state.position}
          duration={state.duration}
          url={src}
          fetchParams={waveformFetchParams}
          onChange={seek}
        />
        <Typography
          variant="caption2"
          tone="muted"
          sx={{ position: "absolute", bottom: "-0.4rem", left: "var(--space-1)" }}
        >
          {formatAudioTime(state.position)} / {formatAudioTime(state.duration)}
        </Typography>
      </Stack>
      <Stack
        className="performance-player-volume"
        direction="row"
        align="center"
        gap="var(--space-1)"
        sx={{ flex: "0 0 auto", width: "auto" }}
        onPointerEnter={() => setVolumeExpanded(true)}
        onPointerLeave={() => setVolumeExpanded(false)}
        onFocusCapture={() => setVolumeExpanded(true)}
        onBlurCapture={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) setVolumeExpanded(false);
        }}
      >
        <IconButton
          icon={state.volume ? Volume2 : VolumeX}
          label={t(state.volume ? "audio.mute" : "audio.unmute")}
          onClick={() => setVolume(state.volume ? 0 : rememberedVolume.current)}
        />
        <Slider
          aria-label={t("audio.recordingVolume")}
          min={0}
          max={1}
          step={0.05}
          value={state.volume}
          showValue={false}
          controlSx={{
            inlineSize: volumeExpanded ? "var(--space-16)" : 0,
            minInlineSize: 0,
            overflow: "hidden",
            opacity: volumeExpanded ? 1 : 0,
            pointerEvents: volumeExpanded ? "auto" : "none",
            transform: volumeExpanded ? "translateX(0)" : "translateX(0.35rem)"
          }}
          onChange={setVolume}
        />
      </Stack>
    </Stack>
  );
}
