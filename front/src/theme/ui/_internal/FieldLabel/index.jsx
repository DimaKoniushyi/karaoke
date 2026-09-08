import cx from "../cx";
import FieldTooltipButton from "../FieldTooltipButton";
import "./field-label.css";

export default function FieldLabel({ htmlFor, label, tooltip, required = false, className }) {
  if (!label) return null;

  return (
    <div className={cx("ui-field-label-row", className)}>
      <label className="ui-field-label" htmlFor={htmlFor}>
        {label}
        {required && (
          <span className="ui-field-required" aria-hidden="true">
            {" "}
            *
          </span>
        )}
      </label>

      <FieldTooltipButton tooltip={tooltip} />
    </div>
  );
}
