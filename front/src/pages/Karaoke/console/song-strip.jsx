import { Mic } from "lucide-react";
import { api } from "../../../api/client";
import useApiFetchParams from "../../../hooks/useApiFetchParams";
import useSongCover from "../../../hooks/useSongCover";
import { translateSaved as t } from "../../../i18n/runtime";
import { Box, Stack, Typography, Waveform } from "../../../theme/ui";
import { formatClockTime as formatTime } from "../../../utils/time-format";

export default function SongStrip({ song, currentTime, duration, onSeek, disablelabel }) {
  const cover = useSongCover(song?.id, `${song?.updated_at ?? ""}:${song?.status ?? ""}`);
  const fetchParams = useApiFetchParams();

  if (!song) return null;

  return (
    <Stack
      direction="row"
      align="center"
      gap="var(--space-3)"
      sx={{
        padding: "var(--space-1) var(--space-3)",
        borderBlockEnd: "1px solid color-mix(in srgb, var(--color-primary) 35%, transparent)",
        background: "color-mix(in srgb, var(--color-bg-deep) 48%, transparent)"
      }}
    >
      {!disablelabel && (
        <>
          <Box
            aria-hidden
            sx={{
              position: "relative",
              flex: "none",
              display: "grid",
              placeItems: "center",
              aspectRatio: 1,
              padding: "var(--space-4)",
              overflow: "hidden",
              borderRadius: "var(--shape-md)",
              color: "var(--color-primary)",
              background: "var(--ui-gradient-surface)"
            }}
          >
            {cover.hasCover ? (
              <Box
                as="img"
                src={cover.coverUrl}
                alt=""
                decoding="async"
                onError={cover.handleCoverError}
                sx={{
                  position: "absolute",
                  inset: 0,
                  width: "100%",
                  height: "100%",
                  objectFit: "cover"
                }}
              />
            ) : (
              <Mic />
            )}
          </Box>

          <Stack gap="var(--space-1)" sx={{ flex: "0 1 12rem", minInlineSize: 0 }}>
            <Typography as="strong" variant="body2" noWrap>
              {song.title}
            </Typography>
            <Typography variant="caption" tone="muted" noWrap>
              {song.artist || song.performer || t("karaoke.karaoke")}
            </Typography>
          </Stack>
        </>
      )}

      <Typography variant="caption">{formatTime(currentTime)}</Typography>

      <Waveform
        label={t("karaoke.songPosition")}
        value={currentTime}
        duration={duration}
        onChange={onSeek}
        url={song.id ? api.getAudioTrackUrl(song.id, "instrumental") : ""}
        fetchParams={fetchParams}
      />

      <Typography variant="caption">{formatTime(duration)}</Typography>
    </Stack>
  );
}
