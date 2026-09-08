import fs from "node:fs";
import path from "node:path";
import { expect, test } from "vitest";
import { same, verify } from "./helpers/assertions.mjs";
const root = process.cwd();
const exists = (relativePath) => fs.existsSync(path.join(root, relativePath));
test("keeps generated coverage consolidated under one canonical directory", () => {
  const legacyCoverage = fs
    .readdirSync(root, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && entry.name.startsWith("coverage-"));
  expect(legacyCoverage).toEqual([]);
  verify([fs.readFileSync(path.join(root, "vitest.config.mjs"), "utf8"), "toContain", 'reportsDirectory: "../generated/coverage/front"']);
  expect(exists("coverage")).toBe(false);
});
test("keeps required production entry files in their canonical locations", () => {
  same(
    [exists("src/runtime-config.js"), true],
    [exists("src/components/PerformanceAnalysisModal/index.jsx"), true],
    [exists("src/pages/MelodyEditor/index.jsx"), true]
  );
});
test("release gate executes unit verification and pins a supported Node runtime", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  const releaseCheck = fs.readFileSync(path.join(root, "scripts/check-release.mjs"), "utf8");
  verify(
    [releaseCheck, "toContain", '["run", "verify"]'],
    [pkg.scripts["build:electron"], "toBe", undefined],
    [pkg.build?.win?.target, "toBe", "dir"],
    [pkg.build?.nsis, "toBe", undefined],
    [pkg.build?.mac, "toBe", undefined],
    [pkg.build?.linux, "toBe", undefined],
    [pkg.engines?.node, "toContain", "22.18.0"]
  );
  same(
    [fs.readFileSync(path.join(root, ".nvmrc"), "utf8").trim(), "22.18.0"],
    [fs.readFileSync(path.join(root, ".node-version"), "utf8").trim(), "22.18.0"]
  );
});
test("mutation gate uses canonical production module ids and pragmatic thresholds", () => {
  const stryker = fs.readFileSync(path.join(root, "stryker.config.mjs"), "utf8");
  verify(
    [stryker, "toContain", "thresholds: { high: 90, low: 75, break: 75 }"],
    [stryker, "not.toContain", '"src/pages/Settings/config.jsx"'],
    [stryker, "not.toContain", '"src/pages/Settings/screens/**/config.js"'],
    [stryker, "not.toContain", '"!src/utils/config.js"']
  );
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  verify([pkg.scripts["test:mutation"], "toBe", "stryker run && node scripts/check-mutation-quality.mjs"]);
  const qualityCheck = fs.readFileSync(path.join(root, "scripts/check-mutation-quality.mjs"), "utf8");
  verify([qualityCheck, "toContain", "const BREAK_SCORE = 75"], [qualityCheck, "toContain", "const COVERED_TARGET = 90"]);
  const testFiles = fs.readdirSync(path.join(root, "tests")).filter((name) => /\.(?:mjs|jsx)$/.test(name));
  const queryImports = [];
  for (const name of testFiles) {
    const source = fs.readFileSync(path.join(root, "tests", name), "utf8");
    if (/\.\.\/src\/[^\n"'`]+\?[^\n"'`]*/.test(source)) queryImports.push(name);
  }
  expect(queryImports).toEqual([]);
});
