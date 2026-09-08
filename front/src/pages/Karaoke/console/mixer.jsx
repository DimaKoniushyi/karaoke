import { Fragment } from "react";
import { Mic } from "lucide-react";
import { translateSaved as t } from "../../../i18n/runtime";
import { Grid, RotaryKnob, Slider, Stack, Switch, Typography } from "../../../theme/ui";
// Paired with EFFECT_FIELDS by array index below (knob/slider column pairs
// in one Grid) -- intentionally shorter than EFFECT_FIELDS, so the render
// loop's `mixer &&` guard leaves the remaining effect knobs without a
// paired slider instead of throwing. Add a mixer slider here only if the
// new effect should render alongside one.
const MIXER_FIELDS = [
  ["microphone", t("karaoke.mick"), "var(--color-primary)"],
  ["music", t("karaoke.music"), "var(--color-success)"],
  ["vocal", t("karaoke.vocals"), "var(--color-warning)"],
  ["melody", t("karaoke.melody"), "var(--color-secondary)"]
];

const EFFECT_FIELDS = [
  ["echo", t("karaoke.echo")],
  ["reverb", t("karaoke.reverb"), "secondary"],
  ["delay", t("karaoke.delay")],
  ["noise_suppression", t("karaoke.noise")],
  ["octave", t("karaoke.voiceOctave"), "secondary", -1, 1, 0.1, 100]
];

const percent = (value) => Math.round((Number(value) || 0) * 100);

export default function MixerPanel({ audio, preferences }) {
  const volumes = {
    microphone: audio.microphoneVolume,
    music: preferences.musicVolume,
    vocal: preferences.vocalVolume,
    melody: preferences.melodyVolume
  };
  const changes = {
    microphone: audio.setMicrophoneVolume,
    music: (value) => preferences.previewPreference("musicVolume", value),
    vocal: (value) => preferences.previewPreference("vocalVolume", value),
    melody: (value) => preferences.previewPreference("melodyVolume", value)
  };
  const commits = {
    microphone: (value) => audio.updateMicrophone({ volume: value }),
    music: preferences.setMusicVolume,
    vocal: preferences.setVocalVolume,
    melody: preferences.setMelodyVolume
  };

  return (
    <Stack gap="var(--space-2)">
      <Stack direction="row" align="center" gap="var(--space-2)">
        <Mic aria-hidden />
        <Typography variant="caption"><strong>{t("karaoke.mixer")}</strong></Typography>
        <Typography variant="caption" tone="muted">
          {percent(Math.max(0, Math.min(1, Number(audio.microphoneLevel) || 0)))}%
        </Typography>
        <Switch
          size="sm"
          variant="plain"
          label={t("karaoke.iHearMyself")}
          checked={!!audio.monitoringEnabled}
          onChange={audio.onMonitoringChange}
        />
        {audio.monitoringEnabled && (
          <Switch
            size="sm"
            variant="plain"
            label={t("karaoke.listenDryVoice")}
            checked={!!audio.dryMonitor}
            onChange={audio.onDryMonitorChange}
          />
        )}
      </Stack>

      <Grid columns={EFFECT_FIELDS.length + MIXER_FIELDS.length} gap="var(--space-2)" align="end">
        {EFFECT_FIELDS.map(([effect, label, accent, min = 0, max = 1, step, factor], index) => {
          const mixer = MIXER_FIELDS[index];
          return (
            <Fragment key={effect}>
              <RotaryKnob
                label={label}
                min={min}
                max={max}
                step={step}
                value={audio.microphoneEffects[effect] ?? 0}
                displayFactor={factor}
                accent={accent}
                onChange={(value) => audio.onEffectChange(effect, value)}
                onCommit={(value) => audio.onEffectCommit(effect, value)}
              />
              {mixer && (
                <Stack align="center" gap="var(--space-1)">
                  <Typography variant="caption" noWrap style={{ color: mixer[2] }}>{mixer[1]}</Typography>
                  <Slider
                    size="sm"
                    orientation="vertical"
                    aria-label={mixer[1]}
                    min={0}
                    max={mixer[0] === "microphone" ? 2 : 1}
                    step={0.05}
                    value={volumes[mixer[0]] ?? 0}
                    showValue={false}
                    controlSx={{ blockSize: "var(--space-16)", minBlockSize: 0 }}
                    onChange={changes[mixer[0]]}
                    onCommit={commits[mixer[0]]}
                  />
                  <Typography variant="caption" style={{ color: mixer[2] }}>
                    {percent(volumes[mixer[0]])}%
                  </Typography>
                </Stack>
              )}
            </Fragment>
          );
        })}
      </Grid>
    </Stack>
  );
}
