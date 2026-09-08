import { useMemo } from "react";
import { flattenLyricsNotes, shiftLyricsSync } from "../../../utils/lyrics-sync";
import { clamp } from "../../../utils/math";
import { formatCompactKey } from "../../../utils/music";
import { transposeKey } from "../utils/data";
import useMelodyGuide from "./useMelodyGuide";

const formatKey = (key) => formatCompactKey(key, "—");

export default function useKaraokeTimeline({
  song,
  lyricsSync,
  timingOffsets,
  setTimingOffsets,
  melodyVolume,
  keyShift,
  speed,
  setSpeed,
  currentTimeRef
}) {
  const notes = useMemo(() => flattenLyricsNotes(lyricsSync), [lyricsSync]);
  const timingKey = [
    song?.id ?? "",
    lyricsSync?.bpm ?? "",
    lyricsSync?.duration ?? "",
    lyricsSync?.words?.[0]?.start ?? ""
  ].join("|");
  const embeddedOffset = clamp(Number(lyricsSync?.alignment?.offset_seconds) || 0, -10, 10);
  const savedOffset = Number(timingOffsets?.[timingKey]);
  const lyricsOffset = Number.isFinite(savedOffset) ? savedOffset : embeddedOffset;
  const displayLyricsSync = useMemo(
    () => shiftLyricsSync(lyricsSync, lyricsOffset - embeddedOffset),
    [embeddedOffset, lyricsOffset, lyricsSync]
  );
  const displayNotes = useMemo(() => flattenLyricsNotes(displayLyricsSync), [displayLyricsSync]);
  const melody = useMelodyGuide({ notes, volume: melodyVolume, keyShift, currentTimeRef });

  const bpm = Number(lyricsSync?.bpm);
  const baseTempo = Number.isFinite(bpm) && bpm > 0 ? bpm : 120;
  const currentTempo = Math.max(1, Math.round(baseTempo * (Number(speed) || 1)));
  const compactKey = lyricsSync?.key
    ? formatKey(transposeKey(lyricsSync.key, keyShift))
    : "";

  return {
    ...melody,
    displayLyricsSync,
    displayNotes,
    lyricsOffset,
    currentTempo,
    compactKey,
    changeTempo: (delta) => setSpeed(clamp((currentTempo + delta) / baseTempo, 0.5, 1.5)),
    changeLyricsOffset: (value) => {
      const next = clamp(Math.round(Number(value) * 10) / 10, -10, 10);
      if (!Number.isFinite(next)) return;
      setTimingOffsets((offsets) => ({ ...offsets, [timingKey]: next }));
    }
  };
}
