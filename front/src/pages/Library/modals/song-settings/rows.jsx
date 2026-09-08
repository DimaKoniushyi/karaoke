import { genreOptions } from "../../../../constants/music-genres";
import { AIModes } from "../../../../constants/utils";
import { translateSaved as tr } from "../../../../i18n/runtime";
import { RenderFormikFields } from "../../../../theme/ui";
import Field from "../../../../theme/ui/_internal/Field";

export default () => [
  {
    xs: 4,
    tag: "artist",
    label: tr("library.sort.artist"),
    placeholder: tr("library.sort.artistPlaceholder")
  },
  {
    xs: 4,
    tag: "title",
    label: tr("library.songTitle"),
    placeholder: tr("library.songTitlePlaceholder")
  },
  { xs: 4, tag: "genre", label: tr("library.genre"), type: "SelectField", options: genreOptions() },
  {
    tag: "tempo_override",
    label: tr("karaoke.pace"),
    placeholder: tr("karaoke.pacePlaceholder"),
    type: "number",
    min: 1,
    xs: 4
  },
  {
    tag: "key_override",
    label: tr("karaoke.key"),
    placeholder: tr("library.egCM"),
    xs: 4
  },

  {
    tag: "difficulty_override",
    label: tr("library.complexity"),
    type: "SelectField",
    options: AIModes,
    xs: 4
  },
  {
    tag: "note_range",
    type: "custom",
    label: tr("library.noteRange"),
    xs: 12,
    render: ({ formik }) => (
      <Field label={tr("library.noteRange")}>
        {({ id }) => (
          <RenderFormikFields
            formik={formik}
            items={["min", "max"].map((edge) => ({
              tag: `note_range_${edge}`,
              type: "NumberField",
              xs: 6,
              id: `${id}-${edge}`,
              min: 0,
              max: 127,
              valueType: "nullable-number",
              placeholder: tr(edge === "min" ? "library.from" : "library.to"),
              "aria-label": tr(edge === "min" ? "library.bottomNote" : "library.topNote")
            }))}
          />
        )}
      </Field>
    )
  },
  {
    tag: "video_url",
    label: tr("library.linkToClip"),
    placeholder: "https://example.com/video.mp4",
    xs: 12
  }
];
