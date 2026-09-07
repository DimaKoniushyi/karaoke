import { Lock, LogOut, Mic, MicOff, Sparkles, Unlock, Volume2, VolumeX } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import { Box, IconButton, Popover, RotaryKnob, Slider, Stack, Typography } from "../theme/ui";
import { formatPercent } from "../utils/math";
import LiveSignalWaveform from "./LiveSignalWaveform";

const key = (enabled, on, off) => `room.person.${enabled ? on : off}`;

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
  const [volumeOpen, setVolumeOpen] = useState(false);
  const volumeAnchorRef = useRef(null);
  const effectsAnchorRef = useRef(null);
  const closeTimerRef = useRef(null);
  const effectsCloseTimerRef = useRef(null);
  const [effectsOpen, setEffectsOpen] = useState(false);
  const [effectDraft, setEffectDraft] = useState({
    volume: 1,
    reverb: 0,
    echo: 0,
    delay: 0,
    noise_suppression: 0.35,
    octave: 0
  });
  const openVolume = () => {
    clearTimeout(closeTimerRef.current);
    setEffectsOpen(false);
    setVolumeOpen(true);
  };
  const closeVolumeSoon = () => {
    clearTimeout(closeTimerRef.current);
    closeTimerRef.current = setTimeout(() => setVolumeOpen(false), 120);
  };
  const openEffects = () => {
    clearTimeout(effectsCloseTimerRef.current);
    setVolumeOpen(false);
    setEffectsOpen(true);
  };
  const closeEffectsSoon = () => {
    clearTimeout(effectsCloseTimerRef.current);
    effectsCloseTimerRef.current = setTimeout(() => setEffectsOpen(false), 120);
  };
  useEffect(
    () => () => {
      clearTimeout(closeTimerRef.current);
      clearTimeout(effectsCloseTimerRef.current);
    },
    []
  );
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

  const effectFields = [
    ["volume", t("settings.appearance.volume.label"), 2, 1, "primary"],
    ["reverb", t("karaoke.reverb"), 1, 0, "secondary"],
    ["echo", t("karaoke.echo"), 1, 0, "primary"],
    ["delay", t("karaoke.delay"), 1, 0, "secondary"],
    ["noise_suppression", t("room.effects.noise_suppression.label"), 1, 0.35, "primary"],
    ["octave", t("karaoke.voiceOctave"), 1, 0, "secondary", -1, 0.1]
  ];
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
