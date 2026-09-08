import { Lock, LogOut, Mic, MicOff, Sparkles, Unlock, Volume2, VolumeX } from "lucide-react";
import { useEffect, useState } from "react";
import useHoverPopover from "../hooks/useHoverPopover";
import { useI18n } from "../i18n";
import { Box, IconButton, Popover, RotaryKnob, Slider, Stack, Typography } from "../theme/ui";
import { formatPercent } from "../utils/math";
import LiveSignalWaveform from "./LiveSignalWaveform";

const key = (enabled, on, off) => `room.person.${enabled ? on : off}`;

// Single source of truth for the six per-participant effect knobs: both the
// default draft values and the rendered RotaryKnob rows are derived from
// this list instead of hand-duplicating the same names/defaults twice.
const EFFECT_FIELD_DEFS = [
  { name: "volume", labelKey: "settings.appearance.volume.label", max: 2, defaultValue: 1, accent: "primary" },
  { name: "reverb", labelKey: "karaoke.reverb", max: 1, defaultValue: 0, accent: "secondary" },
  { name: "echo", labelKey: "karaoke.echo", max: 1, defaultValue: 0, accent: "primary" },
  { name: "delay", labelKey: "karaoke.delay", max: 1, defaultValue: 0, accent: "secondary" },
  {
    name: "noise_suppression",
    labelKey: "room.effects.noise_suppression.label",
    max: 1,
    defaultValue: 0.35,
    accent: "primary"
  },
  { name: "octave", labelKey: "karaoke.voiceOctave", max: 1, defaultValue: 0, accent: "secondary", min: -1, step: 0.1 }
];
const DEFAULT_EFFECT_DRAFT = Object.fromEntries(
  EFFECT_FIELD_DEFS.map(({ name, defaultValue }) => [name, defaultValue])
);

export default function OnlineRoomParticipant({
  person,
  room,
  localSpeakingLevel = 0,
  speakingLevel = 0,
  microphoneMuted = false,
  roomSoundMuted = false,
  isLocallyMuted = false,
  effectsEnabled = false,
  effectsLocked = false,
  effectSettings,
  participantVolume = 1,
  transferStatus,
  onLeave,
  onSetMicrophoneMuted,
  onSetRoomSoundMuted,
  onSetParticipantVolume,
  onSetParticipantEffects,
  onSetEffectsLocked,
  onTogglePersonMuted,
  onTogglePersonEffects
}) {
  const { t } = useI18n();
  const {
    open: volumeOpen,
    setOpen: setVolumeOpen,
    anchorRef: volumeAnchorRef,
    show: showVolume,
    hideSoon: closeVolumeSoon
  } = useHoverPopover();
  const {
    open: effectsOpen,
    setOpen: setEffectsOpen,
    anchorRef: effectsAnchorRef,
    show: showEffects,
    hideSoon: closeEffectsSoon
  } = useHoverPopover();
  const [effectDraft, setEffectDraft] = useState(DEFAULT_EFFECT_DRAFT);
  const openVolume = () => {
    setEffectsOpen(false);
    showVolume();
  };
  const openEffects = () => {
    setVolumeOpen(false);
    showEffects();
  };
  useEffect(() => {
    if (!effectSettings) return;
    setEffectDraft((current) => ({ ...current, ...effectSettings }));
  }, [effectSettings]);

  const self = person.id === room.selfId;

  const inactive = self ? microphoneMuted || roomSoundMuted : person.micMuted;

  const level = inactive ? 0 : self ? localSpeakingLevel : speakingLevel;

  const speaking = level > 0.08;

  const selfActions = [
    [
      microphoneMuted ? MicOff : Mic,
      t(microphoneMuted ? "room.microphone.enable" : "room.microphone.disable"),
      () => onSetMicrophoneMuted(!microphoneMuted),
      roomSoundMuted
    ],
    [
      roomSoundMuted ? VolumeX : Volume2,
      t(roomSoundMuted ? "room.sound.enable" : "room.sound.disable"),
      () => onSetRoomSoundMuted(!roomSoundMuted)
    ],
    [
      effectsLocked ? Lock : Unlock,
      t(effectsLocked ? "room.effects.allow" : "room.effects.deny"),
      () => onSetEffectsLocked?.(!effectsLocked)
    ],
    [LogOut, t("room.leave"), onLeave]
  ];

  const effectFields = EFFECT_FIELD_DEFS.map(
    ({ name, labelKey, max, defaultValue, accent, min = 0, step = 0.05 }) => [
      name,
      t(labelKey),
      max,
      defaultValue,
      accent,
      min,
      step
    ]
  );
  const commitEffect = (name, value) => {
    const next = { ...effectDraft, [name]: value };
    setEffectDraft(next);
    onSetParticipantEffects?.(person.id, next);
  };

  return (
    <Stack
      direction="row"
      align="center"
      justify="space-between"
      gap="var(--space-3)"
      data-self={self || undefined}
      data-speaking={speaking || undefined}
      sx={{ overflow: "visible" }}
    >
      <Stack direction="row" align="center" gap="var(--space-2)" sx={{ flex: 1 }}>
        <Typography as="strong" noWrap sx={{ flex: 1 }}>
          {person.name}
        </Typography>
        {transferStatus && transferStatus.stage !== "error" && (
          <Typography variant="caption" tone="muted">
            {Math.round(transferStatus.percent || 0)}%
          </Typography>
        )}
        <LiveSignalWaveform
          active={!inactive}
          level={level}
          max={1}
          compact
          ariaLabel={t(key(speaking, "speaking", "silent"), {
            name: person.name
          })}
          title={t(key(speaking, "speakingNow", "noSignal"))}
        />
      </Stack>

      <Stack
        direction="row"
        align="center"
        gap="var(--space-2)"
        sx={{ inlineSize: "auto", overflow: "visible", flex: 1 }}
      >
        {self ? (
          selfActions.map(([icon, label, onClick, disabled]) => (
            <IconButton
              key={label}
              icon={icon}
              label={label}
              variant="contained"
              disabled={disabled}
              onClick={onClick}
            />
          ))
        ) : (
          <>
            <Box
              ref={volumeAnchorRef}
              sx={{
                position: "relative",
                display: "inline-flex",
                alignItems: "center",
                overflow: "visible"
              }}
              onMouseEnter={openVolume}
              onMouseLeave={closeVolumeSoon}
            >
              <IconButton
                icon={isLocallyMuted ? VolumeX : Volume2}
                variant={isLocallyMuted ? "contained" : "outlined"}
                label={t(key(isLocallyMuted, "enable", "disable"), {
                  name: person.name
                })}
                onClick={() => onTogglePersonMuted(person.id)}
              />

              <Popover
                open={volumeOpen}
                anchorRef={volumeAnchorRef}
                placement="right"
                onClose={() => setVolumeOpen(false)}
                onMouseEnter={openVolume}
                onMouseLeave={closeVolumeSoon}
                style={{
                  padding: "var(--space-4)",
                  boxShadow: "var(--shadow-lg)"
                }}
              >
                <Slider
                  min={0}
                  max={1}
                  step={0.05}
                  value={participantVolume}
                  formatValue={formatPercent}
                  aria-label={t("room.person.volume", {
                    name: person.name
                  })}
                  onChange={(value) => onSetParticipantVolume?.(person.id, value)}
                  controlSx={{
                    inlineSize: "100%"
                  }}
                />
              </Popover>
            </Box>

            <Box
              ref={effectsAnchorRef}
              sx={{ display: "inline-flex", overflow: "visible" }}
              onMouseEnter={openEffects}
              onMouseLeave={closeEffectsSoon}
            >
              <IconButton
                icon={Sparkles}
                variant={effectsEnabled ? "contained" : "outlined"}
                aria-pressed={effectsEnabled}
                label={t(key(effectsEnabled, "effects.disable", "effects.enable"), {
                  name: person.name
                })}
                onClick={() => onTogglePersonEffects(person.id)}
              />
              <Popover
                open={effectsOpen}
                anchorRef={effectsAnchorRef}
                placement="right"
                onClose={() => setEffectsOpen(false)}
                onMouseEnter={openEffects}
                onMouseLeave={closeEffectsSoon}
                aria-label={t("room.effects.participant", { 0: person.name })}
                style={{
                  width: "min(18rem, calc(100vw - 1rem))",
                  padding: "var(--space-4)",
                  boxShadow: "var(--shadow-lg)"
                }}
              >
                <Stack gap="var(--space-3)">
                  <Typography as="strong">{t("room.effects.title")}</Typography>
                  {effectsLocked && (
                    <Typography variant="caption" tone="muted">
                      {t("room.effects.locked")}
                    </Typography>
                  )}
                  <Stack direction="row" justify="center" gap="var(--space-3)" wrap>
                    {effectFields.map(
                      ([name, label, maximum, defaultValue, accent, minimum = 0, step = 0.05]) => (
                        <RotaryKnob
                          key={name}
                          label={label}
                          min={minimum}
                          max={maximum}
                          step={step}
                          defaultValue={defaultValue}
                          value={effectDraft[name] ?? defaultValue}
                          displayFactor={100}
                          accent={accent}
                          size="md"
                          disabled={effectsLocked}
                          onChange={(value) =>
                            setEffectDraft((current) => ({ ...current, [name]: value }))
                          }
                          onCommit={(value) => commitEffect(name, value)}
                        />
                      )
                    )}
                  </Stack>
                </Stack>
              </Popover>
            </Box>
          </>
        )}
      </Stack>
    </Stack>
  );
}
