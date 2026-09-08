import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import { test } from "vitest";

test("reachability audit follows Vite worker queries without allowlisting live code", () => {
  assert.equal(
    fs.existsSync("src/pages/Library/backdrop/qftRuntime.js"),
    true,
    "the visualizer worker is production code, not an audit suppression",
  );
  const output = execFileSync(process.execPath, ["scripts/audit-reachability.mjs"], {
    encoding: "utf8",
  });
  assert.match(output, /production source files are reachable/);
});
