import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptPath = fileURLToPath(import.meta.url);
const root = path.resolve(path.dirname(scriptPath), "..");
const srcRoot = path.join(root, "src");
const extensions = new Set([".js", ".jsx"]);

export const DEFAULT_COMPLEXITY_BUDGET = Object.freeze({
  lines: 500,
  branches: 80,
  state: 15,
  effects: 12
});

// Existing hotspots get explicit, reviewable ratchets. Raising one of these
// numbers is an architecture decision; new files receive the smaller default.
export const FILE_COMPLEXITY_BUDGETS = Object.freeze({
  // Imported visualizer runtime is intentionally kept intact; ratchet its
  // current reviewed size without weakening the default budget for new code.
  "src/pages/Library/backdrop/qftRuntime.js": { lines: 1286, branches: 145 },
  "src/services/onlineVoiceTransfers.js": { lines: 860, branches: 110 },
  "src/i18n/messages-ru.js": { lines: 832, branches: 0 },
  "src/i18n/messages-uk.js": { lines: 830, branches: 0 },
  "src/i18n/messages-en.js": { lines: 825, branches: 0 },
  // +1 line: a WebRTC offer-glare fix (rollback before setRemoteDescription)
  // that couldn't fit its condition and the awaited call on one line.
  "src/services/onlineVoiceMesh.js": { lines: 661, branches: 100 },
  "src/contexts/OnlineRoomContext.jsx": { lines: 580, branches: 35 },
  "src/pages/Karaoke/index.jsx": { lines: 550, branches: 25 },
  "src/services/onlineRoom.js": { lines: 485, branches: 95 },
  "src/pages/Karaoke/hooks/useKaraokeTransport.js": { lines: 435, branches: 85 }
});

function walk(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const fullPath = path.join(directory, entry.name);
    if (entry.isDirectory()) return fullPath === path.join(srcRoot, "theme") ? [] : walk(fullPath);
    return [fullPath];
  });
}

export function measureComplexity(source) {
  return {
    lines: source.split(/\r?\n/).length,
    state: (source.match(/\buseState\s*\(/g) ?? []).length,
    effects: (source.match(/\buseEffect\s*\(/g) ?? []).length,
    callbacks: (source.match(/\buseCallback\s*\(/g) ?? []).length,
    branches: (source.match(/\b(?:if|switch|case|catch)\b|&&|\?\?/g) ?? []).length
  };
}

export function violationsFor(relativePath, metrics) {
  const override = FILE_COMPLEXITY_BUDGETS[relativePath] ?? {};
  const budget = { ...DEFAULT_COMPLEXITY_BUDGET, ...override };
  return ["lines", "branches", "state", "effects"]
    .filter((metric) => metrics[metric] > budget[metric])
    .map(
      (metric) => `${relativePath}: ${metric}=${metrics[metric]} exceeds budget ${budget[metric]}`
    );
}

export function auditComplexity() {
  const reports = walk(srcRoot)
    .filter((file) => extensions.has(path.extname(file)))
    .map((file) => {
      const relativePath = path.relative(root, file).replaceAll("\\", "/");
      const metrics = measureComplexity(fs.readFileSync(file, "utf8"));
      return { relativePath, metrics, violations: violationsFor(relativePath, metrics) };
    });
  return { reports, violations: reports.flatMap((report) => report.violations) };
}

if (path.resolve(process.argv[1] ?? "") === path.resolve(scriptPath)) {
  const { reports, violations } = auditComplexity();
  const ratchets = reports.filter(({ relativePath }) => FILE_COMPLEXITY_BUDGETS[relativePath]);
  if (violations.length) {
    console.error(
      `Complexity budget violations:\n${violations.map((item) => `- ${item}`).join("\n")}`
    );
    process.exitCode = 1;
  } else {
    console.log(
      `Complexity budgets passed (${reports.length} production files, ${ratchets.length} explicit hotspot ratchets).`
    );
  }
}
