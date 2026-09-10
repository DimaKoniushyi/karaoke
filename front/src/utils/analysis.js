import { translateSaved as t } from "../i18n/runtime";

const EMPTY_SECTIONS = Object.freeze([]);
const PERCENT_FIELDS = [
  "pitch_accuracy_percent",
  "rhythm_accuracy_percent",
  "note_hold_percent",
  "note_coverage_percent",
  "overall_score_percent"
];
const GRADES = [
  [85, "karaoke.excellentPerformance"],
  [70, "karaoke.goodResult"],
  [50, "karaoke.thereIsPotential"],
  [-Infinity, "karaoke.needToPractice"]
];
const ADVICE = {
  pitch: "karaoke.practiceMatchingNotePitchesStartingAtASlowTempo",
  rhythm: "karaoke.practiceYourEntriesListenForTheDownbeatAndStart",
  hold: "karaoke.sustainEachNoteSteadilyToItsEndAndPace",
  coverage: "karaoke.singEveryMarkedPhraseCloserToTheMicrophoneWithout"
};

const finite = (value) => {
  if (value == null || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
};
const percent = (value) => {
  const number = finite(value);
  return number == null ? null : Math.max(0, Math.min(100, number));
};
const extreme = (items, better) =>
  items.reduce((best, item) => (!best || better(item, best) ? item : best), null);

function getGrade(accuracy) {
  if (accuracy == null) return t("common.noData");
  return t(GRADES.find(([minimum]) => accuracy >= minimum)[1]);
}

function getAdvice(accuracy, deviation, metric) {
  if (accuracy == null) return t("karaoke.couldnTDetermineEnoughNotesSungTrySingingCloser");
  if (deviation > 1) return t("karaoke.focusOnStartingEachPhraseAccuratelyAndMaintainingThe");
  if (ADVICE[metric?.key]) return t(ADVICE[metric.key]);
  return t(
    accuracy >= 70
      ? "karaoke.goodAccuracyTryToMakeThePhrasesMoreEven"
      : "karaoke.repeatDifficultPhrasesMoreSlowlyFocusingOnTheNotes"
  );
}

export function normalizeAnalysisSection(section, index = 0) {
  const source = section && typeof section === "object" ? section : {};
  const start = finite(source.start);
  const end = finite(source.end);
  return {
    ...source,
    label: typeof source.label === "string" && source.label.trim() ? source.label.trim() : null,
    start,
    end: start == null || end >= start ? end : null,
    accuracy_percent: percent(source.accuracy_percent),
    mean_deviation_semitones: finite(source.mean_deviation_semitones),
    index
  };
}

export function normalizeAnalysisResult(result) {
  const source = result && typeof result === "object" ? result : {};
  const normalized = { ...source };
  PERCENT_FIELDS.forEach((key) => {
    normalized[key] = percent(source[key]);
  });
  normalized.mean_deviation_semitones = finite(source.mean_deviation_semitones);
  normalized.sections = Array.isArray(source.sections)
    ? source.sections.filter((section) => section && typeof section === "object").map(normalizeAnalysisSection)
    : EMPTY_SECTIONS;
  return normalized;
}

export function getAnalysisFeedback(result) {
  const normalized = normalizeAnalysisResult(result);
  const accuracy = normalized.overall_score_percent ?? normalized.pitch_accuracy_percent;
  const metrics = [
    ["pitch", normalized.pitch_accuracy_percent],
    ["rhythm", normalized.rhythm_accuracy_percent],
    ["hold", normalized.note_hold_percent],
    ["coverage", normalized.note_coverage_percent]
  ]
    .filter(([, value]) => value != null)
    .map(([key, value]) => ({ key, value }));
  const practiceMetric = metrics.length > 1 ? extreme(metrics, (a, b) => a.value < b.value) : null;
  const scoredSections = normalized.sections.filter(
    ({ accuracy_percent }) => accuracy_percent != null
  );
  const bestSection = extreme(scoredSections, (a, b) => a.accuracy_percent > b.accuracy_percent);
  const needsPractice = extreme(scoredSections, (a, b) => a.accuracy_percent < b.accuracy_percent);

  return {
    ...normalized,
    accuracy,
    metrics,
    practiceMetric,
    scoredSections,
    bestSection,
    needsPractice,
    grade: getGrade(accuracy),
    advice: getAdvice(accuracy, normalized.mean_deviation_semitones, practiceMetric)
  };
}
