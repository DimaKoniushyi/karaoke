import { Pause, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toggleAudioPlayback } from "../../../../components/audio-player-utils";
import { translateSaved as tr } from "../../../../i18n/runtime";
import { IconButton } from "../../../../theme/ui";

export default ({ file }) => {
  const ref = useRef(null);
  const [playing, setPlaying] = useState(false);
  const canPreview = Boolean(file && typeof URL.createObjectURL === "function");
  useEffect(() => {
    if (!canPreview) return;
    const audio = ref.current;
    const url = URL.createObjectURL(file);
    audio.src = url;
    return () => {
      audio.pause();
      URL.revokeObjectURL(url);
    };
  }, [canPreview, file]);
  const Icon = playing ? Pause : Play;
  const stop = () => setPlaying(false);
  return (
    <>
      <IconButton
        label={tr(playing ? "library.pauseSelectedAudioFile" : "library.previewSelectedAudioFile")}
        variant={playing ? "contained" : "outlined"}
        size="lg"
        disabled={!canPreview}
        onClick={async () => setPlaying(await toggleAudioPlayback(ref.current))}
      >
        <Icon size={19} fill={playing ? "currentColor" : "none"} />
      </IconButton>
      <audio
        ref={ref}
        preload="metadata"
        onPlay={() => setPlaying(true)}
        onPause={stop}
        onEnded={stop}
      />
    </>
  );
};
