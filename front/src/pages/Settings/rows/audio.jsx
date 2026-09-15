import { Volume2 } from "lucide-react";
import LiveSignalWaveform from "../../../components/LiveSignalWaveform";
import { translateSaved } from "../../../i18n/runtime";
import { Stack, Switch } from "../../../theme/ui";
import { formatPercent } from "../../../utils/math";

export default function audioRows({ settings: { audio }, run, tr = translateSaved }) {
  const status = audio.monitorStatus;
  const running = status?.state === "running";
  const input = status?.input_latency_ms;
  const output = status?.output_latency_ms;
  const known = [input, output].every((value) => Number.isFinite(value) && value >= 0);
  const source = status?.latency_source === "asio-driver-report" ? "driver" : "estimate";
  // Priority: an actually-measured mic-to-speaker round trip beats the
  // driver-reported input+output estimate, which is a coarser fallback for
  // hosts/engines that don't report a real measurement at all. Two engines
  // can supply the measured figure -- real_latency_ms (PortAudio engines,
  // timestamped via inputBufferAdcTime/outputBufferDacTime) and
  // stream_latency_ms for the native WASAPI engine (timestamped via the
  // device's own audio clock in monitor.cpp's render_ready()) -- both cover
  // the same driver-visible capture-to-playback path. An effect that reads
  // signal history (currently pitch shifting) adds algorithmic delay which
  // driver timestamps cannot observe, so add its separately reported bound.
  const transportMeasuredMs =
    Number.isFinite(status?.real_latency_ms) && status.real_latency_ms > 0
      ? status.real_latency_ms
      : status?.latency_source === "wasapi-stream-report" &&
          Number.isFinite(status?.stream_latency_ms) &&
          status.stream_latency_ms > 0
        ? status.stream_latency_ms
        : null;
  const effectLatencyMs =
    Number.isFinite(status?.effect_latency_ms) && status.effect_latency_ms >= 0
      ? status.effect_latency_ms
      : 0;
  const measuredMs =
    transportMeasuredMs == null ? null : transportMeasuredMs + effectLatencyMs;
  const measured = measuredMs != null;
  const latency = measured
    ? tr("settings.audio.monitor.compact.measured", { 0: measuredMs.toFixed(3) })
    : known
      ? tr(`settings.audio.monitor.compact.${source}`, {
          0: (input + output).toFixed(3),
          1: input.toFixed(3),
          2: output.toFixed(3)
        })
      : tr("settings.audio.monitor.compact.unavailable");
  const negotiatedPeriod =
    Number.isFinite(status?.input_period_frames) && Number.isFinite(status?.output_period_frames)
      ? tr("settings.audio.monitor.compact.negotiatedPeriod", {
          0: status.input_period_frames,
          1: status.output_period_frames,
          2: status?.sample_rate ?? "—"
        })
      : null;
  return [
    ...[
      ["output_device_id", "settings.audio.output_device_id.label", "outputs"],
      ["input_device_id", "settings.audio.input_device_id.label", "inputs"]
    ].map(([name, label, list]) => ({
      md: 4,
      type: "SelectField",
      tag: `audio.${name}`,
      label: tr(label),
      options: audio.options?.[list] ?? [],
      valueType: "nullable-number"
    })),
    {
      md: 4,
      type: "ButtonField",
      label: tr("settings.audio.speakerTest.label"),
      startIcon: <Volume2 />,
      variant: "outlined",
      disabled: audio.busy,
      onClick: () => run(audio.speaker)
    },
    ...[["volume", "settings.audio.volume.label", 2]].map(([name, label, max]) => ({
      type: "Slider",
      md: 4,
      tag: `audio.${name}`,
      label: tr(label),
      min: 0,
      max,
      step: 0.05,
      formatValue: formatPercent
    })),
    {
      md: 4,
      render: () => (
        <Stack direction="row" align="center" gap={1}>
          <LiveSignalWaveform
            active={status?.state === "running"}
            level={audio.level}
            ariaLabel={tr("settings.audio.microphoneLevel")}
          />
          <Switch
            label={tr("settings.audio.monitoringEnabled.label")}
            variant="plain"
            checked={!!audio.values?.monitoring_enabled}
            disabled={audio.busy}
            onChange={() => run(() => audio.monitor())}
          />
        </Stack>
      )
    },
    {
      md: 4,
      type: "SelectField",
      tag: "audio.buffer_size",
      tooltip: tr("settings.audio.buffer_size.description"),
      valueType: "number",
      label: tr("settings.audio.buffer_size.label"),
      options: [16, 32, 48, 64, 96, 128, 256, 512, 1024, 2048].map((value) => ({
        value,
        label: value
      }))
    },
    {
      md: 12,
      type: "SelectField",
      tag: "audio.asio_driver_name",
      label: tr("settings.audio.audio_driver.label"),
      onSave: audio.selectDriver,
      options: audio.options?.drivers ?? []
    },
    {
      type: "Label",
      role: "alert",
      variant: "caption",
      showFor: !!status?.error,
      text: status?.error
    },
    {
      md: 12,
      type: "Label",
      role: "alert",
      variant: "caption",
      // A failed ASIO driver silently falls back to shared Windows audio
      // (see configure_monitoring's "fallback" event) -- without this, the
      // driver dropdown still shows the ASIO choice while the app is
      // actually running on a completely different audio path, and any
      // latency comparison against it is meaningless.
      showFor: running && !!status?.failed_driver,
      text: tr("settings.audio.monitor.asioFallback", {
        0: status?.failed_driver,
        1: status?.fallback_reason || ""
      })
    },
    {
      type: "Label",
      role: "alert",
      variant: "caption",
      showFor: !!audio.monitorStatusError,
      text: tr("settings.audio.monitor.status.unavailable")
    },
    {
      type: "Label",
      role: "status",
      variant: "caption",
      showFor: ["starting", "stopping"].includes(status?.state),
      text: tr(`settings.audio.monitor.status.${status?.state ?? "checking"}`)
    },

    {
      md: 12,
      type: "Label",
      variant: "caption",
      tone: "muted",
      // buffer_size above is only what was requested; Windows/the driver can
      // negotiate a different period, and that's what actually applies.
      showFor: running && !!negotiatedPeriod,
      text: negotiatedPeriod
    },
    {
      md: 6,
      type: "Label",
      variant: "caption",
      showFor: running,
      title: tr(`settings.audio.monitor.compact.${measured ? "measured" : source}Tooltip`),
      text: latency
    },
    {
      md: 6,
      type: "Label",
      variant: "caption",
      showFor: running,
      text: tr("settings.audio.monitor.compact.driverName", {
        0:
          status?.mode === "ASIO"
            ? status?.driver || "ASIO"
            : [status?.host_api, status?.mode].filter(Boolean).join(" · ") || "—"
      })
    }
  ];
}
