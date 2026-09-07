import { translateSaved as tr } from "../../../../../i18n/runtime";
import { Stack, Typography } from "../../../../../theme/ui";
import { formatSafeDate } from "../../../../../utils/time-format";
import { formatEta } from "../../../utils";

const finishTime = (value) =>
  formatSafeDate(value, (date) =>
    date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
  );
export default ({ state, stage, active, current }) => {
  return (
    <Stack gap={0.4}>
      <Stack direction="row" justify="space-between" gap={1}>
        <Typography>
          {state === "done"
            ? tr("library.theSongIsReadyForKaraoke")
            : state === "cancelled"
              ? tr("library.processingCancelled")
              : stage || tr("library.preparingTheSongProcessing")}
        </Typography>

        {active && (
          <Typography sx={{ fontWeight: 800 }}>
            {current?.eta_seconds == null
              ? tr("library.estimatingTime")
              : `${tr("library.left")} ${formatEta(current.eta_seconds)}`}
          </Typography>
        )}
      </Stack>

      {active && current?.stage_elapsed_seconds != null && (
        <Typography variant="caption" tone="muted">
          {[
            ["library.currentStage", current.stage_elapsed_seconds],
            ["library.total", current.total_elapsed_seconds]
          ]
            .map(([label, value]) => `${tr(label)} ${formatEta(value)}`)
            .concat(
              `${tr("library.estimatedCompletion")} ${finishTime(current.estimated_finish_at)}`
            )
            .join(" · ")}
        </Typography>
      )}
    </Stack>
  );
};
