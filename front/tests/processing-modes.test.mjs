import { describe, expect, test } from "vitest";
import { AIModes } from "../src/constants/utils.js";

describe("song processing modes", () => {
  test("exposes every supported mode with a distinct user-facing label", () => {
    expect(AIModes.map(({ value }) => value)).toEqual(["auto", "fast", "quality"]);
    expect(new Set(AIModes.map(({ label }) => label)).size).toBe(3);
    for (const mode of AIModes) {
      expect(mode.label.trim()).not.toBe("");
      expect(mode.description.trim()).not.toBe("");
    }
  });

});
