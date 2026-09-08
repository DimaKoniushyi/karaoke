import { describe, expect, test } from "vitest";
import {
  auditComplexity,
  DEFAULT_COMPLEXITY_BUDGET,
  FILE_COMPLEXITY_BUDGETS,
  measureComplexity,
  violationsFor
} from "../scripts/audit-complexity.mjs";
import { ARCHITECTURE_HOTSPOTS, hotspotDependencyEdges } from "../../scripts/ownership-map.mjs";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");

describe("production complexity budgets", () => {
  test("the current tree stays within fixed ratchets", () => {
    expect(auditComplexity().violations).toEqual([]);
  });

  test.each([
    ["lines", `${"const value = 1;\n".repeat(DEFAULT_COMPLEXITY_BUDGET.lines)}\n`],
    ["branches", `export const value = ${"true && ".repeat(DEFAULT_COMPLEXITY_BUDGET.branches + 1)}true;`],
    ["state", `function C(){${"useState();".repeat(DEFAULT_COMPLEXITY_BUDGET.state + 1)}}`],
    ["effects", `function C(){${"useEffect(()=>{});".repeat(DEFAULT_COMPLEXITY_BUDGET.effects + 1)}}`]
  ])("rejects a new file over its %s budget", (metric, source) => {
    const violations = violationsFor("src/new-production-module.js", measureComplexity(source));
    expect(violations).toHaveLength(1);
    expect(violations[0]).toContain(`${metric}=`);
  });

  test("hotspot allowances are named and cannot silently become a global exception", () => {
    expect(Object.keys(FILE_COMPLEXITY_BUDGETS)).toContain("src/pages/Library/backdrop/qftRuntime.js");
    expect(violationsFor("src/copied-qft-runtime.js", { lines: 501, branches: 0, state: 0, effects: 0 })).toEqual([
      "src/copied-qft-runtime.js: lines=501 exceeds budget 500"
    ]);
  });
});

describe("hotspot interfaces and dependency graph", () => {
  test.each(ARCHITECTURE_HOTSPOTS)("$id exposes its declared public seam", (hotspot) => {
    const source = fs.readFileSync(path.join(repositoryRoot, hotspot.path), "utf8");
    expect(hotspot.publicInterfaces.length).toBeGreaterThan(0);
    for (const marker of hotspot.publicInterfaces) expect(source).toContain(marker);
  });

  test("all dependency edges resolve and the hotspot graph is acyclic", () => {
    const ids = new Set(ARCHITECTURE_HOTSPOTS.map(({ id }) => id));
    const edges = hotspotDependencyEdges();
    for (const [, target] of edges) expect(ids.has(target)).toBe(true);

    const visiting = new Set();
    const visited = new Set();
    const visit = (id) => {
      if (visiting.has(id)) throw new Error(`hotspot dependency cycle at ${id}`);
      if (visited.has(id)) return;
      visiting.add(id);
      for (const [source, target] of edges) if (source === id) visit(target);
      visiting.delete(id);
      visited.add(id);
    };
    for (const id of ids) visit(id);
    expect(visited).toEqual(ids);
  });
});
