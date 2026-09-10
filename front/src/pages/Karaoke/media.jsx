import { memo, useContext, useEffect, useState } from "react";
import { api } from "../../api/client";
import { AppSettingsContext } from "../../contexts/app-settings";
import { observeLightingMedia } from "../../services/keyboardLighting";
import { Box } from "../../theme/ui";
import * as platform from "../../utils/platform";
import { noop } from "./utils/async";
import { playbackGain } from "./utils/data";
import { normalizePlaybackRate } from "./utils/transport";

const cleanup = (file) => Promise.resolve().then(() => file?.cleanup?.()).catch(noop);

function useTrack(songId, track) {
  const desktop = platform.isElectron();
  const [blobUrl, setBlobUrl] = useState("");

  useEffect(() => {
    if (desktop || !songId) return setBlobUrl("");

    let active = true;
    let url;
    let file;
    setBlobUrl("");

    api
      .getAudioTrackBlob(songId, track)
      .then((blob) => {
        file = blob;
        if (!active) return cleanup(blob);
        try {
          url = URL.createObjectURL(blob);
          setBlobUrl(url);
        } catch {
          cleanup(blob);
          file = null;
          if (active) setBlobUrl("");
        }
      })
      .catch(() => active && setBlobUrl(""));

    return () => {
      active = false;
      if (url) URL.revokeObjectURL(url);
      cleanup(file);
    };
  }, [desktop, songId, track]);

  return desktop && songId ? api.getAudioTrackUrl(songId, track) : blobUrl;
}

function AudioTrack({ audioRef, songId, track, volume }) {
  const settings = useContext(AppSettingsContext)?.settings;
  const src = useTrack(songId, track);

  useEffect(() => {
    const audio = audioRef.current;
    if (audio) audio.volume = playbackGain(volume);
  }, [audioRef, src, volume]);

  useEffect(() => {
    const audio = audioRef.current;
    if (
      audio &&
      track === "instrumental" &&
      settings?.keyboard_lighting_enabled &&
      settings?.keyboard_lighting_mode === "music"
    ) {
      return observeLightingMedia(audio);
    }
  }, [audioRef, src, track, settings?.keyboard_lighting_enabled, settings?.keyboard_lighting_mode]);

  useEffect(() => {
    const audio = audioRef.current;
    return () => audio?.pause();
  }, [audioRef]);

  return (
    <Box
      as="audio"
      ref={audioRef}
      src={src || undefined}
      crossOrigin="anonymous"
      preload="auto"
      sx={{ display: "none" }}
    />
  );
}

function KaraokeMedia({
  instrumentalRef,
  isPlaying,
  musicVolume,
  song,
  speed,
  syncSecondaryMedia,
  videoRef,
  vocalVolume,
  vocalsRef,
  onClipAvailabilityChange = noop
}) {
  const [clipFailed, setClipFailed] = useState(false);
  const songId = song?.id;
  const clip = songId && song.video_url === "local:clip" ? api.getSongVideoUrl(songId) : "";

  useEffect(() => {
    setClipFailed(false);
    onClipAvailabilityChange(false);
  }, [clip, onClipAvailabilityChange]);

  return (
    <>
      {songId &&
        [
          ["instrumental", instrumentalRef, musicVolume],
          ["vocals", vocalsRef, vocalVolume]
        ].map(([track, ref, volume]) => (
          <AudioTrack key={track} audioRef={ref} songId={songId} track={track} volume={volume} />
        ))}

      {clip && !clipFailed && (
        <Box
          as="video"
          ref={videoRef}
          src={clip}
          preload="auto"
          muted
          playsInline
          onLoadedData={(event) => {
            const video = event.currentTarget;
            video.playbackRate = normalizePlaybackRate(speed);
            syncSecondaryMedia?.(instrumentalRef.current?.currentTime || 0, true);
            onClipAvailabilityChange(true);
            if (isPlaying) Promise.resolve().then(() => video.play()).catch(noop);
          }}
          onError={() => {
            setClipFailed(true);
            onClipAvailabilityChange(false);
          }}
          sx={{
            position: "absolute",
            inset: 0,
            width: "100%",
            height: "100%",
            objectFit: "cover",
            zIndex: 0,
            pointerEvents: "none"
          }}
        />
      )}
    </>
  );
}

export default memo(KaraokeMedia);
