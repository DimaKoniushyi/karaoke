import { translateSaved } from "../../../i18n/runtime";
import ModelStatus from "../ModelStatus";

export default function processingRows({ tr = translateSaved } = {}) {
  return [
    {
      type: "SelectField",
      tag: "compute_mode",
      label: tr("settings.ai.compute_mode.label"),
      options: [
        ["auto", "settings.option.compute_mode.auto"],
        ["cuda", "settings.option.compute_mode.cuda"],
        ["cpu", "settings.option.compute_mode.cpu"]
      ].map(([value, label]) => ({ value, label: tr(label) }))
    },
    {
      type: "NumberField",
      tag: "thread_count",
      label: tr("settings.ai.thread_count.label"),
      min: 1,
      max: 64,
      step: 1,
      validate: (value) =>
        Number.isInteger(value) && value >= 1 && value <= 64
          ? undefined
          : tr("settings.ai.thread_count.validation")
    },
    ...[
      ["songs_folder", "settings.ai.songs_folder.label"],
      ["ai_folder", "settings.ai.ai_folder.label"],
      ["cache_folder", "settings.ai.cache_folder.label"]
    ].map(([tag, label]) => ({
      type: "FolderField",
      tag,
      label: tr(label),
      md: tag === "cache_folder" ? 12 : 6,
      browseLabel: tr("settings.ai.folder.browseLabel", { 0: tr(label) })
    })),
    { md: 12, render: () => <ModelStatus /> }
  ];
}
