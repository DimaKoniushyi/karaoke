import { translateSaved } from "../../../i18n/runtime";
import { formatPercent } from "../../../utils/math";

const appearanceRows = ({ settings: { radio, lighting }, tr = translateSaved }) => [
  {
    tag: "online_name",
    label: tr("settings.appearance.online_name.label"),
    tooltip: tr("settings.appearance.online_name.tooltip"),
    placeholder: tr("settings.appearance.online_name.placeholder"),
    maxLength: 40
  },
  {
    type: "SelectField",
    tag: "language",
    label: tr("settings.appearance.language.label"),
    tooltip: tr("settings.appearance.language.tooltip"),
    options: [
      { value: "uk", label: tr("settings.option.language.uk") },
      { value: "ru", label: tr("settings.option.language.ru") },
      { value: "en", label: tr("settings.option.language.en") }
    ]
  },
  {
    type: "SelectField",
    tag: "theme",
    label: tr("settings.appearance.theme.label"),
    tooltip: tr("settings.appearance.theme.tooltip"),
    options: [
      ["dark", "settings.option.theme.dark"],
      ["light", "settings.option.theme.light"],
      ["green", "settings.option.theme.green"],
      ["violet", "settings.option.theme.violet"]
    ].map(([value, label]) => ({ value, label: tr(label) }))
  },
  {
    type: "SwitchField",
    tag: "radio.enabled",
    label: tr("settings.appearance.enabled.label"),
    tooltip: tr("settings.appearance.enabled.tooltip"),
    checkedText: tr("settings.appearance.enabled.checkedText"),
    uncheckedText: tr("settings.appearance.enabled.uncheckedText"),
    onSave: (value) => (value ? radio.turnOn() : radio.turnOff())
  },
  {
    type: "SelectField",
    tag: "radio.stationId",
    label: tr("settings.appearance.stationId.label"),
    tooltip: tr("settings.appearance.stationId.tooltip"),
    options: (radio.stations ?? []).map(({ id: value, name: label, ...rest }) => ({
      value,
      label,
      ...rest
    })),
    onSave: radio.setStation
  },
  {
    type: "Slider",
    tag: "radio.volume",
    label: tr("settings.appearance.volume.label"),
    tooltip: tr("settings.appearance.volume.tooltip"),
    min: 0,
    max: 1,
    step: 0.01,
    formatValue: formatPercent,
    onSave: radio.setVolume
  },
  {
    type: "SwitchField",
    tag: "keyboard_lighting_enabled",
    label: tr("settings.appearance.keyboard_lighting_enabled.label"),
    tooltip: tr("settings.appearance.keyboard_lighting_enabled.tooltip")
  },
  {
    type: "SelectField",
    tag: "keyboard_lighting_mode",
    label: tr("settings.appearance.keyboard_lighting_mode.label"),
    options: ["music", "theme"].map((value) => ({
      value,
      label: tr(`settings.appearance.keyboard_lighting_mode.${value}`)
    }))
  },
  {
    type: "Slider",
    tag: "keyboard_lighting_brightness",
    label: tr("settings.appearance.keyboard_lighting_brightness.label"),
    min: 0,
    max: 1,
    step: 0.05,
    formatValue: formatPercent
  },
  {
    type: "Slider",
    tag: "keyboard_lighting_sensitivity",
    label: tr("settings.appearance.keyboard_lighting_sensitivity.label"),
    tooltip: tr("settings.appearance.keyboard_lighting_sensitivity.tooltip"),
    min: 0.25,
    max: 2,
    step: 0.05,
    formatValue: formatPercent
  },
  {
    type: "Label",
    md: 12,
    variant: "caption",
    text: tr(`settings.appearance.keyboard_lighting_status.${lighting?.state || "disabled"}`, {
      count: lighting?.count || 0,
      provider:
        {
          windows: "Windows Dynamic Lighting",
          usb: "USB · HATOR / Wooting / VialRGB",
          openrgb: "OpenRGB"
        }[lighting?.provider] || "USB"
    })
  }
];
export default appearanceRows;
