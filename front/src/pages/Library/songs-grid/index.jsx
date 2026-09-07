import { useMemo } from "react";
import { useDropzone } from "react-dropzone";
import { translateSaved as tr } from "../../../i18n/runtime";
import { Box, Card, Grid, Stack, Typography } from "../../../theme/ui";
import { sameId, SONG_DROPZONE_ACCEPT } from "../utils";
import LibrarySongCard from "./card";

export const selectSongTransferStatus = (statuses, id) => {
  const all = statuses.filter((status) => sameId(status?.songId, id));
  return (
    all.find(({ stage }) => stage === "error") ||
    all
      .filter(({ participantId }) => participantId && participantId !== "room")
      .sort((a, b) => a.percent - b.percent)[0] ||
    all.at(-1)
  );
};

export default function LibrarySongsGrid({
  songs,
  error,
  transferStatuses,
  canManageLibrary,
  fileImport,
  processing,
  recordings,
  openKaraoke,
  onOpenSettings,
  songActions
}) {
  // Grouped by song id once per transferStatuses change, instead of each
  // song re-filtering the whole statuses list in the render loop below.
  const statusesBySong = useMemo(() => {
    const grouped = new Map();
    for (const status of transferStatuses?.values?.() || []) {
      if (status?.songId == null) continue;
      const key = String(status.songId);
      const bucket = grouped.get(key);
      if (bucket) bucket.push(status);
      else grouped.set(key, [status]);
    }
    return grouped;
  }, [transferStatuses]);
  const { getRootProps, isDragActive } = useDropzone({
    accept: SONG_DROPZONE_ACCEPT,
    disabled: fileImport.importing || !canManageLibrary,
    onDropAccepted: fileImport.importFile,
    multiple: true,
    noClick: true,
    noKeyboard: true
  });

  if (error || !songs.length) {
    return (
      <Stack align="center" justify="center" sx={{ minHeight: "30vw" }}>
        <Card
          variant="laser"
          sx={{ textAlign: "center" }}
          cardContent={{ style: { padding: "var(--space-16)" } }}
        >
          <Typography variant="h4" tone={error ? "danger" : "muted"} role={error ? "alert" : undefined}>
            {error
              ? `${tr("library.failedToLoadList")} ${error.message || error}`
              : tr("library.thereAreNoSongsYetAddTheFirstOne")}
          </Typography>
        </Card>
      </Stack>
    );
  }

  return (
    <Box
      {...getRootProps()}
      aria-label={tr("library.songDropZone")}
      data-drop-active={isDragActive || undefined}
    >
      <Grid columns={3} gap="var(--space-6)" align="start">
        {songs.map((song, cardIndex) => (
          <LibrarySongCard
            key={song.id}
            song={song}
            cardIndex={cardIndex}
            canManageLibrary={canManageLibrary}
            transferStatus={selectSongTransferStatus(
              statusesBySong.get(String(song.id)) || [],
              song.id
            )}
            onOpenKaraoke={openKaraoke}
            onOpenProcessing={processing.track}
            onOpenRecordings={recordings.setSong}
            onOpenSettings={onOpenSettings}
            onDelete={songActions.deleteSong}
            onOpenFolder={songActions.openSongFolder}
            onProcess={songActions.processSong}
            onReprocess={songActions.reprocessSong}
          />
        ))}
      </Grid>
    </Box>
  );
}
