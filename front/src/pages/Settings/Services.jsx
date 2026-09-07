import {
  CheckCircle2,
  CircleAlert,
  Database,
  Info,
  ListChecks,
  Stethoscope,
  Trash2
} from "lucide-react";
import { useState } from "react";
import { api } from "../../api/client";
import { useAppDialog } from "../../contexts/AppDialog";
import { usePolling } from "../../hooks/usePolling";
import { useI18n } from "../../i18n";
import { translateSaved as tr } from "../../i18n/runtime";
import { POLLING_INTERVALS as POLL } from "../../runtime-config";
import { Button, Card, Chip, Grid, Select, Stack, Typography } from "../../theme/ui";
import { getErrorMessage } from "../../utils/errors";
import { formatSafeDate } from "../../utils/time-format";
import { SERVICES } from "./schema";

export const SERVICE_ICONS = {
  memory: Database,
  history: ListChecks,
  diagnostics: Stethoscope,
  about: Info
};

const PANEL = { padding: "1rem", border: "1px solid var(--ui-border)", borderRadius: "1rem" };
const LOCALE = { uk: "uk-UA", ru: "ru-RU", en: "en-US" };
const TONE = { done: "success", error: "danger" };
const CLEAR = [
  ["clearCache", api.clearCache],
  ["deleteTemporaryFiles", api.deleteTemp]
];

const useData = (fn, interval, fallback) => usePolling(fn, interval, []).data ?? fallback;

const ErrorText = ({ error }) =>
  error && (
    <Typography tone="danger" variant="body2">
      {getErrorMessage(error)}
    </Typography>
  );

const Metrics = ({ items }) => (
  <Grid columns={2} gap="var(--space-2)">
    {items.map(([label, value]) => (
      <Card key={label} sx={PANEL}>
        <Typography variant="caption" tone="muted">
          {label}
        </Typography>
        <Typography sx={{ fontWeight: 800, overflowWrap: "anywhere" }}>{value ?? "—"}</Typography>
      </Card>
    ))}
  </Grid>
);

const Empty = ({ children }) => <Typography tone="muted">{children}</Typography>;

function About() {
  const { t } = useI18n();
  const { data: response, error } = usePolling(api.getAbout, POLL.about, []);
  const data = response ?? {};
  const fields = ["backendVersion", "frontendVersion", "aiVersion", "dataPath"];
  const values = [data.backend_version, data.frontend_version, data.ai_version, data.data_dir];

  return (
    <Stack align="center" gap={1.2}>
      <ErrorText error={error} />
      <Typography variant="h2">A&amp;D Voice</Typography>
      <Typography tone="muted">{t("settings.about.description")}</Typography>
      <Metrics items={fields.map((key, i) => [t(`settings.about.${key}`), values[i]])} />
      <Typography variant="caption" tone="muted">
        © 2026 A&amp;D Voice
      </Typography>
    </Stack>
  );
}

function Diagnostics() {
  const { t } = useI18n();
  const health = useData(api.getHealth, POLL.health);
  const pipeline = useData(api.getPipelineHealth, POLL.health, {});
  const versions = useData(api.getVersions, POLL.versions, {});
  const errors = useData(api.getErrors, POLL.errors, {}).errors ?? [];

  return (
    <Stack gap={1}>
      <Grid columns={2} gap="var(--space-2)">
        {[["backend", !!health], ...Object.entries(pipeline)].map(([name, ok]) => {
          const Icon = ok ? CheckCircle2 : CircleAlert;
          return (
            <Card key={name} sx={PANEL}>
              <Stack direction="row" align="center" justify="space-between">
                <Typography>{t(`settings.diagnostics.${name}`, {}, name)}</Typography>
                <Icon size={18} color={`var(--ui-${ok ? "success" : "danger"})`} />
              </Stack>
            </Card>
          );
        })}
      </Grid>

      <Metrics items={Object.entries(versions.components ?? {})} />
      <Typography variant="h3">{t("settings.diagnostics.errors")}</Typography>

      {errors.length ? (
        errors.map(({ id, updated_at, title, error_message }) => (
          <Card key={id ?? `${updated_at}-${title}`} sx={PANEL}>
            <Stack gap={0.25}>
              <Stack direction="row" justify="space-between">
                <Typography>{title}</Typography>
                <Typography variant="caption" tone="muted">
                  {updated_at}
                </Typography>
              </Stack>
              <Typography tone="danger" variant="body2">
                {error_message}
              </Typography>
            </Stack>
          </Card>
        ))
      ) : (
        <Empty>{t("settings.diagnostics.noErrors")}</Empty>
      )}
    </Stack>
  );
}

export const formatDate = (value, language) =>
  formatSafeDate(value, (date) => date.toLocaleString(LOCALE[language]));

function History() {
  const { language, t } = useI18n();
  const { data: response, error } = usePolling(api.getHistory, POLL.history, []);
  const data = response ?? [];

  return (
    <Stack gap={0.75}>
      <ErrorText error={error} />
      {data.length ? (
        data.map(({ id, timestamp, song_title, kind, status }, i) => (
          <Card key={id ?? `${timestamp}-${i}`} sx={PANEL}>
            <Stack direction="row" align="center" justify="space-between" gap={1}>
              <Stack gap={0.2}>
                <Typography>{song_title ?? "—"}</Typography>
                <Typography variant="caption" tone="muted">
                  {t(`settings.history.${kind}`, {}, kind ?? "—")} ·{" "}
                  {formatDate(timestamp, language)}
                </Typography>
              </Stack>
              <Chip tone={TONE[status] ?? "default"}>
                {t(`status.${status}`, {}, status ?? "—")}
              </Chip>
            </Stack>
          </Card>
        ))
      ) : (
        <Empty>{t("settings.history.empty")}</Empty>
      )}
    </Stack>
  );
}

export const formatBytes = (value) => {
  const bytes = +value || 0;
  if (!bytes) return tr("settings.0B");
  const unit = Math.min(3, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / 1024 ** unit).toFixed(1)} ${tr(`settings.${["b", "kb", "mb", "gb"][unit]}`)}`;
};

function Memory() {
  const { alert } = useAppDialog();
  const { data: size, error } = usePolling(api.getCacheSize, POLL.memory, []);
  const free = useData(api.getFreeSpace, POLL.freeSpace, {});
  const songs = useData(api.listSongs, POLL.songs, []);
  const [song, setSong] = useState("");

  const run = async (fn, format) => {
    try {
      await alert(format(await fn()));
    } catch (e) {
      await alert(getErrorMessage(e));
    }
  };

  const freed = (value) => `${tr("settings.freed")}: ${value}`;
  const options = [
    { value: "", label: tr("settings.selectASong") },
    ...songs
      .filter(({ status, optimized }) => status === "done" && !optimized)
      .map(({ id: value, title: label }) => ({ value, label }))
  ];

  return (
    <Stack gap={1}>
      <ErrorText error={error} />
      <Metrics
        items={[
          [tr("settings.memory.total"), size?.total_human],
          [tr("settings.memory.free"), free.free_human]
        ]}
      />
      <Metrics items={Object.entries(size?.breakdown ?? {}).map(([k, v]) => [k, formatBytes(v)])} />

      <Stack direction="row" wrap gap={0.75}>
        {CLEAR.map(([key, fn]) => (
          <Button
            key={key}
            variant="outlined"
            startIcon={<Trash2 />}
            onClick={() => run(fn, ({ freed_bytes }) => freed(formatBytes(freed_bytes)))}
          >
            {tr(`settings.${key}`)}
          </Button>
        ))}
      </Stack>

      <Grid columns={2} gap="var(--space-2)" align="end">
        <Select
          label={tr("settings.history.song")}
          value={song}
          options={options}
          onChange={setSong}
        />
        <Button
          variant="contained"
          disabled={!song}
          onClick={() =>
            run(
              () => api.optimizeSong(song),
              (r) => freed(r?.freed_human ?? "—")
            )
          }
        >
          {tr("settings.memory.optimize")}
        </Button>
      </Grid>
    </Stack>
  );
}

const SCREENS = { memory: Memory, history: History, diagnostics: Diagnostics, about: About };

export function ServiceCards({ open }) {
  const { t } = useI18n();
  return (
    <Grid columns={2} gap="var(--space-2)" sx={{ padding: "1rem" }}>
      {SERVICES.map((id) => {
        const Icon = SERVICE_ICONS[id];
        return (
          <Card
            key={id}
            as="button"
            interactive
            variant="neon"
            tilt={false}
            onClick={() => open(id)}
            sx={{ cursor: "pointer", textAlign: "left", padding: 0 }}
            cardContent={{ style: PANEL }}
          >
            <Stack gap={0.35}>
              <Icon size={20} />
              <Typography sx={{ fontWeight: 800 }}>{t(`settings.service.${id}.title`)}</Typography>
              <Typography tone="muted" variant="body2">
                {t(`settings.service.${id}.text`)}
              </Typography>
            </Stack>
          </Card>
        );
      })}
    </Grid>
  );
}

export function Service({ id }) {
  const Screen = SCREENS[id];
  return (
    Screen && (
      <Stack gap={1} sx={{ padding: "1.25rem" }}>
        <Screen />
      </Stack>
    )
  );
}
