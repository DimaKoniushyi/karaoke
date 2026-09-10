import { Maximize2, Minus, X } from "lucide-react";
import { useI18n } from "../i18n";
import { IconButton, Stack } from "../theme/ui";
import { windowActions } from "../utils/platform";

const actions = [
  ["minimize", "common.minimizeWindow", Minus],
  ["toggleFullscreen", "common.maximizeWindow", Maximize2],
  ["close", "common.closeWindow", X, true]
];

export default function TitleBar({ hideActions = false, title = "A&D Voice" }) {
  const { t } = useI18n();
  if (hideActions) return null;
  const invoke = (action) =>
    Promise.resolve(windowActions[action]?.()).catch((error) =>
      console.error(`Window action failed: ${action}`, error)
    );

  return (
    <Stack
      as="header"
      aria-label={title}
      direction="row"
      justify="end"
      sx={{ minBlockSize: "var(--space-1)", WebkitAppRegion: "drag", zIndex: 1 }}
    >
      {actions.map(([action, key, Icon, danger]) => (
        <IconButton
          key={action}
          icon={Icon}
          label={t(key)}
          onClick={() => invoke(action)}
          variant="ghost"
          sx={{
            color: danger ? "var(--color-danger)" : "var(--color-text-muted)",
            borderTopLeftRadius: 0,
            WebkitAppRegion: "no-drag",
            borderTopRightRadius: 0
          }}
        />
      ))}
    </Stack>
  );
}
