/* @vitest-environment jsdom */
import { fireEvent, render, waitFor } from "@testing-library/react";
import { expect, test } from "vitest";
import createSongFields from "../src/pages/Library/modals/song-settings/rows.jsx";
import { RenderFormikFields, useGetForm } from "../src/theme/ui";

test("song fields use the common Formik contract and responsive cell sizes", () => {
  expect(createSongFields().map(({ tag }) => tag)).toEqual([
    "artist",
    "title",
    "tempo_override",
    "key_override",
    "genre",
    "difficulty_override",
    "note_range",
    "video_url"
  ]);
  for (const field of createSongFields()) {
    expect(field.xs).toBe(12);
    expect([4, 6, 12]).toContain(field.md);
    expect(field).not.toHaveProperty("getValue");
    expect(field).not.toHaveProperty("setValue");
    expect(field).not.toHaveProperty("onChange");
  }
});

test("note range uses central Formik binding, accessible labels and nullable numbers", async () => {
  let form;
  function Example() {
    form = useGetForm({ initialValues: { note_range_min: 40, note_range_max: 80 }, onSubmit() {} });
    return <RenderFormikFields formik={form} items={[createSongFields().find(({ tag }) => tag === "note_range")]} />;
  }
  const view = render(<Example />);
  const min = view.getByLabelText("Нижняя нота"),
    max = view.getByLabelText("Верхняя нота");
  expect(min.value).toBe("40");
  expect(max.value).toBe("80");
  expect(min.min).toBe("0");
  expect(max.max).toBe("127");
  fireEvent.change(min, { target: { value: "" } });
  fireEvent.change(max, { target: { value: "90" } });
  await waitFor(() => expect(form.values).toEqual({ note_range_min: null, note_range_max: 90 }));
});
