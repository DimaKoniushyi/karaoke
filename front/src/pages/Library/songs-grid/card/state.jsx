import { api } from "../../../../api/client";
import useApiFetchParams from "../../../../hooks/useApiFetchParams";
import { translateSaved as tr } from "../../../../i18n/runtime";
import { Button, ProcessingSignal, Stack, Typography } from "../../../../theme/ui";
import { formatSongKey } from "../../utils";

export default function SongState({ song, transfer, isWorking, onOpenProcessing, openKaraoke }) {
  const fetchParams = useApiFetchParams();

  if (!isWorking && !transfer) {
    return (
      <Typography variant="body2" tone="muted">
        {[
          formatSongKey(song.key_override),
          song.tempo_override && tr("common.bpm", { 0: song.tempo_override }),
          song.difficulty_override
        ]
          .filter(Boolean)
          .join(" · ")}
      </Typography>
    );
  }

  const retry = ["error", "cancelled"].includes(transfer?.stage);
  return (
    <Button
      variant="outlined"
      fullWidth
      sx={{ flex: 1, background: "unset", border: "unset", boxShadow: "unset" }}
      onClick={(event) => {
        event.stopPropagation();
        if (isWorking) onOpenProcessing(song);
        else if (retry) openKaraoke();
      }}
    >
      {retry ? (
        <Typography variant="body2" tone="danger">
          {tr("library.transferInterruptedClickToRetry")}
        </Typography>
      ) : (
        <Stack gap="var(--space-1)" sx={{ width: "100%" }}>
          {transfer && (
            <Typography variant="body2" tone="muted">
              {tr(
                `library.${
                  song.__roomLocal ? "waitingForOtherParticipantsToReceiveTheSong" : "downloadingSong"
                }`
              )}
            </Typography>
          )}
          <ProcessingSignal
            compact
            progress={transfer?.percent ?? song.progress_percent}
            url={isWorking ? api.getAudioTrackUrl(song.id, "song") : undefined}
            fetchParams={fetchParams}
          />
        </Stack>
      )}
    </Button>
  );
}
