import { CircleDot, Library, OctagonX, Play } from "lucide-react";
import { api } from "../../../../api/client";
import useApiFetchParams from "../../../../hooks/useApiFetchParams";
import useSongCover from "../../../../hooks/useSongCover";
import { translateSaved as tr } from "../../../../i18n/runtime";
import ProcessingSignal from "../../../../theme/features/ProcessingSignal";
import { Button, Chip, Modal, Stack } from "../../../../theme/ui";
import { getProcessingProgress, isProcessingActive, sameId } from "../../utils";
import getProcessingFailureInfo from "./processing-failure-info";
import QueueNav from "./queue-nav";
import Failure from "./statuses/failure";
import Progress from "./statuses/progress";

export default function ProcessingModal({
  song,
  songs = [],
  status,
  onCancel,
  onClose,
  onOpenKaraoke,
  onSelectSong
}) {
  const current = status?.song_id && !sameId(status.song_id, song?.id) ? null : status;
  const state = current?.status ?? song?.status;
  const cover = useSongCover(song?.id, state);
  const fetchParams = useApiFetchParams();
  if (!song) return null;
  const progress = getProcessingProgress(current, song);
  const active = isProcessingActive(state);
  const stage = current?.progress_detail || current?.progress_step || song.progress_step;
  const queue = songs.some(({ id }) => sameId(id, song.id)) ? songs : [song, ...songs];
  const index = queue.findIndex(({ id }) => sameId(id, song.id));
  const failure =
    state === "error"
      ? getProcessingFailureInfo(current?.error_message ?? song.error_message)
      : null;

  return (
    <Modal
      isOpen
      onClose={onClose}
      ariaLabel={tr("library.songProcessing", { 0: song.title })}
      titleProps={{
        icon: CircleDot,
        image: cover.hasCover ? cover.coverUrl : undefined,
        onImageError: cover.handleCoverError,
        eyebrow: tr("library.songProcessing2"),
        title: song.title,
        description: tr("library.followThePreparationStagesAndManageTheSongProcessing"),
        actions: (
          <>
            {active && (
              <Button variant="contained" startIcon={<OctagonX />} onClick={onCancel}>
                {tr("library.cancel")}
              </Button>
            )}

            {state === "done" && (
              <>
                <Button variant="outlined" startIcon={<Library />} onClick={onClose}>
                  {tr("library.toTheLibrary")}
                </Button>
                <Button
                  variant="contained"
                  startIcon={<Play />}
                  onClick={() => onOpenKaraoke(song)}
                >
                  {tr("library.open")}
                </Button>
              </>
            )}
          </>
        )
      }}
    >
      <Stack gap={1} sx={{ padding: "var(--space-5)" }}>
        <QueueNav song={song} queue={queue} index={index} onSelect={onSelectSong} />
        <Chip
          tone={{ error: "danger", done: "success" }[state] || "default"}
          sx={{ alignSelf: "flex-start" }}
        >
          {tr(state === "cancelling" ? "library.cancelling" : `status.${state || "pending"}`)}
        </Chip>
        {!["error", "cancelled"].includes(state) && (
          <ProcessingSignal
            progress={state === "done" ? 100 : Math.max(active ? 1 : 0, progress)}
            url={
              !/\.(?:kar|mid|kfn)$/i.test(song.original_filename || "") || progress >= 34
                ? api.getAudioTrackUrl(song.id, "song")
                : undefined
            }
            fetchParams={fetchParams}
          />
        )}
        {failure ? (
          <Failure failure={failure} stage={stage} progress={progress} />
        ) : (
          <Progress state={state} stage={stage} active={active} current={current} />
        )}
      </Stack>
    </Modal>
  );
}
