import { useEffect, useId, useRef, useState } from "react";
import { clamp } from "../../../utils/math";
import "./rotary-knob.css";
import { getRotaryDragValue, getRotaryPointerValue, getRotaryWheelValue } from "./utils";

export default function RotaryKnob({
  label,
  value = 0,
  min = 0,
  max = 1,
  step = 0.05,
  defaultValue,
  onChange,
  onCommit,
  accent = "primary",
  size = "lg",
  disabled = false,
  displayFactor
}) {
  const id = `rotary-knob-${useId().replace(/:/g, "")}`;
  const root = useRef(null);
  const drag = useRef(null);
  const valueRef = useRef(0);
  const [draft, setDraft] = useState(null);

  const current = clamp(Number(value) || 0, min, max);
  const range = max - min || 1;
  const ratio = (current - min) / range;
  const percent = Math.round(ratio * 100);
  const factor = Number.isFinite(displayFactor) && displayFactor !== 0 ? displayFactor : null;
  const display = factor ? Math.round(current * factor) : percent;
  const resetValue = defaultValue ?? clamp(0, min, max);

  useEffect(() => {
    if (!drag.current) valueRef.current = current;
  }, [current]);

  const change = (value) => {
    const number = Number(value);
    if (disabled || !Number.isFinite(number)) return valueRef.current;

    const next = clamp(number, min, max);
    valueRef.current = next;
    onChange?.(next);
    return next;
  };

  const commit = (value) => {
    if (disabled) return;

    const next = change(value);
    onCommit?.(next);
    return next;
  };

  useEffect(() => {
    const node = root.current;
    if (!node) return;

    const onWheel = (event) => {
      if (disabled) return;

      event.preventDefault();

      commit(
        getRotaryWheelValue({
          value: valueRef.current,
          deltaY: event.deltaY,
          step,
          min,
          max,
          fine: event.shiftKey
        })
      );
    };

    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [disabled, min, max, step, onChange, onCommit]);

  const stopDrag = () => {
    if (drag.current) onCommit?.(drag.current.value);
    drag.current = null;
  };

  const saveDraft = () => {
    const number = draft?.trim() === "" ? NaN : Number(draft);

    if (Number.isFinite(number)) {
      commit(factor ? number / factor : min + (clamp(number, 0, 100) / 100) * range);
    }

    setDraft(null);
  };

  return (
    <label
      ref={root}
      htmlFor={id}
      className={`ui-rotary-knob ui-rotary-knob--${accent} ui-control`}
      data-size={size}
      data-disabled={disabled || undefined}
      aria-disabled={disabled || undefined}
      style={{
        display: "flex",
        flexDirection: "column",
        touchAction: "none",
        userSelect: "none",
        "--dial-value": `${percent}%`,
        "--dial-progress": `${ratio * 75}%`,
        "--dial-angle": `${ratio * 270 - 135}deg`
      }}
      onPointerDown={(event) => {
        if (
          disabled ||
          event.button !== 0 ||
          event.target.closest("input, .ui-rotary-knob__value")
        ) {
          return;
        }

        event.preventDefault();
        event.currentTarget.setPointerCapture?.(event.pointerId);

        const control = event.target.closest(".ui-rotary-knob__control");
        const next = control
          ? change(
              getRotaryPointerValue({
                clientX: event.clientX,
                clientY: event.clientY,
                rect: control.getBoundingClientRect(),
                min,
                max
              })
            )
          : valueRef.current;

        drag.current = {
          value: next,
          lastY: event.clientY
        };
      }}
      onPointerMove={(event) => {
        if (!drag.current) return;

        drag.current.value = change(
          getRotaryDragValue({
            value: drag.current.value,
            lastY: drag.current.lastY,
            clientY: event.clientY,
            min,
            max,
            fine: event.shiftKey
          })
        );

        drag.current.lastY = event.clientY;
      }}
      onPointerUp={stopDrag}
      onPointerCancel={stopDrag}
      onLostPointerCapture={stopDrag}
    >
      <span className="ui-rotary-knob__label">{label}</span>

      <span
        className="ui-rotary-knob__control"
        aria-hidden
        onDoubleClick={(event) => {
          event.preventDefault();
          event.stopPropagation();

          if (!disabled) commit(resetValue);
        }}
      >
        <span className="ui-rotary-knob__knob" />
      </span>

      <span className="ui-rotary-knob__value">
        {draft !== null ? (
          <input
            autoFocus
            className="ui-control"
            data-size="xs"
            type="text"
            inputMode="decimal"
            value={draft}
            aria-label={label}
            onFocus={(event) => event.target.select()}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={saveDraft}
            onKeyDown={(event) => {
              if (event.key === "Enter") event.currentTarget.blur();
              if (event.key === "Escape") setDraft(null);
            }}
          />
        ) : (
          <strong
            onDoubleClick={(event) => {
              event.preventDefault();
              event.stopPropagation();

              if (!disabled) setDraft(String(display));
            }}
          >
            {display}%
          </strong>
        )}
      </span>

      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={current}
        disabled={disabled}
        aria-label={label}
        aria-valuetext={`${display}%`}
        onChange={(event) => commit(event.target.value)}
        style={{
          position: "absolute",
          width: 1,
          height: 1,
          overflow: "hidden",
          clipPath: "inset(50%)",
          pointerEvents: "none"
        }}
      />
    </label>
  );
}
