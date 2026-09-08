import { Info } from "lucide-react";
import { translateSaved as tr } from "../../../i18n/runtime";
import IconButton from "../IconButton";
import Tooltip from "../Tooltip";

export default function FieldTooltipButton({ tooltip }) {
  if (!tooltip) return null;
  return (
    <Tooltip title={tooltip} placement="top">
      <IconButton
        type="button"
        variant="ghost"
        size="sm"
        aria-label={tr("common.field.more")}
        onMouseDown={(event) => event.preventDefault()}
      >
        <Info size={14} />
      </IconButton>
    </Tooltip>
  );
}
