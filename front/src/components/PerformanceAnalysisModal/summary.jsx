import { translateSaved as t } from "../../i18n/runtime";
import { getAnalysisFeedback } from "../../pages/Karaoke/utils/analysis";
import { Card, Grid, Stack, Typography } from "../../theme/ui";

export default ({ result }) => {
  const feedback = getAnalysisFeedback(result);
  const metrics = [
    [
      "pitch",
      t("karaoke.hittingTheNotes"),
      feedback.pitch_accuracy_percent,
      t("karaoke.accurateNotesWithinHalfASemitone")
    ],
    [
      "rhythm",
      t("karaoke.rhythmAndEntries"),
      feedback.rhythm_accuracy_percent,
      t("karaoke.noteOnsetAccuracyRelativeToTheBackingTrack")
    ],
    [
      "hold",
      t("karaoke.noteSustain"),
      feedback.note_hold_percent,
      t("karaoke.stablePitchThroughoutEachNote")
    ],
    [
      "coverage",
      t("karaoke.performanceCompleteness"),
      feedback.note_coverage_percent,
      t("karaoke.proportionOfSongNotesWithDetectedVoice")
    ]
  ];
  return (
    <Stack align="center" gap="var(--space-4)">
      <Grid columns={2} gap="var(--space-3)">
        {metrics.map(([key, label, value, description]) => (
          <Card key={key} data-practice={feedback.practiceMetric?.key === key || undefined}>
            <Stack gap="var(--space-1)" sx={{ padding: "var(--space-3)" }}>
              <Stack direction="row" align="baseline" justify="space-between" gap="var(--space-2)">
                <Typography>
                  <strong>{label}</strong>
                </Typography>
                <Typography variant="h4">{value == null ? "—" : `${value}%`}</Typography>
              </Stack>
              <Typography variant="caption" tone="muted">
                {description}
              </Typography>
            </Stack>
          </Card>
        ))}
      </Grid>
      <Card variant="laser" tilt={false} cardContent={{ style: { padding: "var(--space-4)" } }}>
        <Stack align="center" gap="var(--space-1)">
          <Typography variant="h4" textAlign="center">
            {feedback.grade}
          </Typography>
          <Typography data-role="analysis-score" variant="h3">
            {feedback.accuracy == null ? "—" : `${feedback.accuracy}%`}
          </Typography>
          <Typography tone="muted">{t("karaoke.overallPerformanceScore")}</Typography>
          <Typography variant="caption" tone="muted" textAlign="center">
            {t("karaoke.totalNotes50Rhythm25Sustain15Completeness10")}
          </Typography>
        </Stack>
        <Stack gap="var(--space-2)">
          <Typography>
            <strong>{t("karaoke.recommendation")}</strong>
          </Typography>
          <Typography tone="muted">{feedback.advice}</Typography>
          {feedback.needsPractice && (
            <Typography variant="caption" tone="muted">
              {t("karaoke.mostDifficultSection", { 0: feedback.needsPractice.accuracy_percent })}
            </Typography>
          )}
        </Stack>
      </Card>
    </Stack>
  );
};
