import { ChevronLeft, ChevronRight, Minus, Pause, Play, Plus, SkipBack, SkipForward, Square } from "lucide-react";
import { translateSaved as t } from "../../../i18n/runtime";
import { Card, Grid, IconButton, Stack, Typography } from "../../../theme/ui";
import { clamp } from "../../../utils/math";

const NOTES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"];

const noteRangeLabel = (value, fallback, shift = 0) => {
  if (value == null || value === "") return fallback;
  const midi = Number(value);
  if (!Number.isFinite(midi)) return String(value);
  const note = clamp(Math.round(midi) + (Number(shift) || 0), 0, 127);
  return `${NOTES[note % 12]}${Math.floor(note / 12) - 1}`;
};

function Metric({ label, value, tone, previous = Minus, next = Plus, previousLabel, nextLabel, onPrevious, onNext }) {
  const step = (icon, text, action) =>
    action && <IconButton icon={icon} label={text} title={text} size="sm" variant="outline" onClick={action} />;

  return (
    <Card tilt={false} style={{ "--card-border": tone }}>
      <Stack align="center" gap="var(--space-1)" sx={{ padding: "var(--space-2) 0" }}>
        <Typography variant="caption" style={{ color: tone }}>{label}</Typography>
        <Stack direction="row" align="center" justify="space-around">
          {step(previous, previousLabel, onPrevious)}
          <Typography variant="body2"><strong>{value}</strong></Typography>
          {step(next, nextLabel, onNext)}
        </Stack>
      </Stack>
    </Card>
  );
}

export default function ConsoleCenter({
  song,
  isPlaying,
  timeline,
  preferences,
  transport,
  onTogglePlay,
  onStop
}) {
  const shift = Number(preferences.keyShift) || 0;
  const controls = [
    [SkipBack, t("karaoke.back5Seconds"), () => transport.skip(-5), "outline"],
    [isPlaying ? Pause : Play, t(isPlaying ? "audio.pause" : "karaoke.play"), onTogglePlay, null, 60],
    [Square, t("karaoke.stop"), onStop, "outline"],
    [SkipForward, t("karaoke.forward5Seconds"), () => transport.skip(5), "outline"]
  ];
  const metrics = [
    {
      label: t("karaoke.pace"),
      value: `${timeline.currentTempo} BPM`,
      tone: "var(--color-primary)",
      previousLabel: t("karaoke.decreaseTempoBy1Bpm"),
      nextLabel: t("karaoke.increaseTempoBy1Bpm"),
      onPrevious: () => timeline.changeTempo(-1),
      onNext: () => timeline.changeTempo(1)
    },
    {
      label: t("karaoke.key"),
      value: timeline.compactKey,
      tone: "var(--color-success)",
      previous: ChevronLeft,
      next: ChevronRight,
      previousLabel: t("karaoke.lowerKey"),
      nextLabel: t("karaoke.raiseTheKey"),
      onPrevious: () => preferences.setKeyShift(clamp(shift - 1, -12, 12)),
      onNext: () => preferences.setKeyShift(clamp(shift + 1, -12, 12))
    },
    {
      label: t("karaoke.range"),
      value: `${noteRangeLabel(song?.note_range_min, "C2", shift)} – ${noteRangeLabel(song?.note_range_max, "C5", shift)}`,
      tone: "var(--color-warning)"
    }
  ];

  return (
    <Stack align="center" justify="space-between" gap="var(--space-2)">
      <Stack direction="row" align="center" justify="center" gap="var(--space-2)">
        {controls.map(([icon, label, onClick, variant, iconSize]) => (
          <IconButton key={label} {...{ icon, label, variant, iconSize, onClick }} />
        ))}
      </Stack>
      <Grid columns={metrics.length} gap="var(--space-2)">
        {metrics.map((metric) => <Metric key={metric.label} {...metric} />)}
      </Grid>
    </Stack>
  );
}
