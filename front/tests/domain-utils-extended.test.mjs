import assert from "node:assert/strict";
import { describe, test, vi } from "vitest";
import { translateSaved } from "../src/i18n/runtime.js";
import { getAnalysisFeedback, normalizeAnalysisResult, normalizeAnalysisSection } from "../src/pages/Karaoke/utils/analysis.js";
import { createPanoramaPath, getYouTubeVideoId, playbackGain, transposeKey, youTubeEmbedUrl } from "../src/pages/Karaoke/utils/data.js";
import {
  createBrowserDeviceOptions,
  createBufferSizeOptions,
  createIndexedDeviceOptions,
  createInputDeviceOptions
} from "../src/pages/Karaoke/utils/devices.js";
import {
  detectMidiFromAnalyser,
  findBestPitchLag,
  getPitchLagRange,
  hasSufficientPitchEnergy,
  isConfidentPitchScore,
  normalizedCorrelation,
  pitchFrequencyToMidi,
  refinePitchLag,
  rootMeanSquare,
  selectFundamentalLag
} from "../src/pages/Karaoke/utils/pitch.js";
import { formatSongKey } from "../src/pages/Library/utils.js";
import { createSongPayload, normalizeText, validateSongSettings } from "../src/pages/Library/modals/song-settings/utils.js";
import { equal, deepEqual } from "./helpers/assertions.mjs";
describe("analysis normalization and feedback", () => {
  test("normalizes every nullable section field without inventing data", () => {
    deepEqual(
      [
        normalizeAnalysisSection(null, 2),
        {
          label: null,
          start: null,
          end: null,
          accuracy_percent: null,
          mean_deviation_semitones: null,
          index: 2
        }
      ],
      [
        normalizeAnalysisSection({
          label: " ",
          start: "",
          end: "",
          accuracy_percent: "bad",
          mean_deviation_semitones: ""
        }),
        {
          label: null,
          start: null,
          end: null,
          accuracy_percent: null,
          mean_deviation_semitones: null,
          index: 0
        }
      ],
      [
        normalizeAnalysisSection({
          label: 7,
          start: null,
          end: 2,
          accuracy_percent: -2,
          mean_deviation_semitones: "-0.5"
        }),
        {
          label: null,
          start: null,
          end: 2,
          accuracy_percent: 0,
          mean_deviation_semitones: -0.5,
          index: 0
        }
      ]
    );
    equal(
      [normalizeAnalysisSection({ start: 2, end: 2 }).end, 2],
      [normalizeAnalysisSection({ start: 3, end: 2 }).end, null],
      [normalizeAnalysisSection({ start: null, end: -1 }).end, -1],
      [normalizeAnalysisSection({ label: " best " }).label, "best"]
    );
    for (const section of [false, "bad", 7])
      deepEqual([
        normalizeAnalysisSection(section),
        {
          label: null,
          start: null,
          end: null,
          accuracy_percent: null,
          mean_deviation_semitones: null,
          index: 0
        }
      ]);
  });
  test("normalizes result shape and removes malformed sections", () => {
    for (const result of [null, false, "bad"])
      deepEqual([
        normalizeAnalysisResult(result),
        {
          pitch_accuracy_percent: null,
          rhythm_accuracy_percent: null,
          note_hold_percent: null,
          note_coverage_percent: null,
          overall_score_percent: null,
          mean_deviation_semitones: null,
          sections: []
        }
      ]);
    deepEqual([normalizeAnalysisResult({ sections: "bad" }).sections, []]);
    const normalized = normalizeAnalysisResult({
      pitch_accuracy_percent: 120,
      rhythm_accuracy_percent: 72,
      note_hold_percent: 88,
      note_coverage_percent: 91,
      overall_score_percent: 84,
      mean_deviation_semitones: "2",
      sections: [null, "bad", { label: " best ", start: 1, end: 2, accuracy_percent: 90 }, { start: 3, end: 2, accuracy_percent: -2 }]
    });
    equal(
      [normalized.pitch_accuracy_percent, 100],
      [normalized.rhythm_accuracy_percent, 72],
      [normalized.note_hold_percent, 88],
      [normalized.note_coverage_percent, 91],
      [normalized.overall_score_percent, 84],
      [normalized.mean_deviation_semitones, 2],
      [normalized.sections.length, 2],
      [normalized.sections[0].label, "best"],
      [normalized.sections[0].index, 0],
      [normalized.sections[1].end, null]
    );
  });
  test("selects scored, best and weakest sections with stable tie order", () => {
    const firstBest = { label: "first-best", accuracy_percent: 80 };
    const secondBest = { label: "second-best", accuracy_percent: 80 };
    const firstWorst = { label: "first-worst", accuracy_percent: 20 };
    const secondWorst = { label: "second-worst", accuracy_percent: 20 };
    const middle = { label: "middle", accuracy_percent: 50 };
    const feedback = getAnalysisFeedback({
      pitch_accuracy_percent: 70,
      sections: [{ label: "unscored" }, middle, firstBest, firstWorst, secondBest, secondWorst]
    });
    equal([feedback.scoredSections.length, 5]);
    deepEqual([feedback.scoredSections.map(({ label }) => label), ["middle", "first-best", "first-worst", "second-best", "second-worst"]]);
    equal([feedback.bestSection.label, "first-best"], [feedback.needsPractice.label, "first-worst"]);
    const empty = getAnalysisFeedback({ sections: [] });
    equal([empty.bestSection, null], [empty.needsPractice, null]);
    deepEqual([empty.scoredSections, []]);
  });
  test("uses exact grade and advice boundaries", () => {
    const feedback = (accuracy, deviation) =>
      getAnalysisFeedback({
        pitch_accuracy_percent: accuracy,
        mean_deviation_semitones: deviation
      });
    const noData = feedback(null, 2);
    const poor = feedback(49.99, 0);
    const potential = feedback(50, 0);
    const good = feedback(70, 0);
    const excellent = feedback(85, 0);
    equal(
      [noData.grade, translateSaved("Нет данных")],
      [poor.grade, translateSaved("Нужно потренироваться")],
      [potential.grade, translateSaved("Есть потенциал")],
      [good.grade, translateSaved("Хороший результат")],
      [excellent.grade, translateSaved("Отличное исполнение")],
      [feedback(84.99, 0).grade, translateSaved("Хороший результат")],
      [feedback(69.99, 0).grade, translateSaved("Есть потенциал")],
      [noData.advice, translateSaved("Не удалось определить достаточно пропетых нот. Попробуйте петь ближе к микрофону.")],
      [poor.advice, translateSaved("Повторите сложные фразы медленнее, ориентируясь на ноты на экране.")],
      [feedback(70, null).advice, translateSaved("Хорошая точность. Попробуйте сделать фразы ровнее по громкости и дыханию.")],
      [feedback(70, 1).advice, feedback(70, null).advice],
      [feedback(70, 1.01).advice, translateSaved("Сфокусируйтесь на точном начале каждой фразы и удержании высоты ноты.")],
      [feedback(69.99, 0).advice, poor.advice]
    );
  });
});
describe("karaoke data contracts", () => {
  test("parses keys, gain and YouTube URLs", () => {
    equal([transposeKey("Db minor", 2), "D# minor"]);
    const sharpKeys = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
    sharpKeys.forEach((expected, shift) => assert.equal(transposeKey(" C ", shift), expected));
    for (const [root, expected] of [
      ["C", "C"],
      ["C#", "C#"],
      ["Db", "C#"],
      ["D", "D"],
      ["D#", "D#"],
      ["Eb", "D#"],
      ["E", "E"],
      ["F", "F"],
      ["F#", "F#"],
      ["Gb", "F#"],
      ["G", "G"],
      ["G#", "G#"],
      ["Ab", "G#"],
      ["A", "A"],
      ["A#", "A#"],
      ["Bb", "A#"],
      ["B", "B"]
    ])
      equal([transposeKey(root, 0), expected]);
    equal(
      [transposeKey("C", -1), "B"],
      [transposeKey("C", 13), "C#"],
      [transposeKey("H", 1), "H"],
      [transposeKey("xC", 1), "xC"],
      [transposeKey("Cb", 1), "Cb"],
      [transposeKey("", 1).length > 0, true],
      [transposeKey("C", "bad"), "C"],
      [playbackGain(0.5), 0.25],
      [playbackGain(-2), 0],
      [playbackGain("bad"), 0]
    );
    for (const url of [
      "https://youtu.be/abcdefghijk",
      "http://youtu.be/abcdefghijk",
      "https://www.youtube.com/watch?v=abcdefghijk",
      "https://youtube-nocookie.com/embed/abcdefghijk",
      "https://privacy.youtube-nocookie.com/live/abcdefghijk",
      "https://m.youtube.com/shorts/abcdefghijk",
      "https://youtube.com/embed/abcdefghijk",
      "https://evilwww.youtube.com/watch?v=abcdefghijk"
    ])
      equal([getYouTubeVideoId(url), "abcdefghijk"]);
    for (const url of [
      null,
      "",
      "bad",
      "ftp://youtu.be/abcdefghijk",
      "https://example.com/abcdefghijk",
      "https://youtube.com/watch?v=xabcdefghijk",
      "https://youtube.com/watch?v=abcdefghijkx",
      "https://youtube.com/watch",
      "https://youtube.com/prefix/embed/abcdefghijk"
    ])
      equal([getYouTubeVideoId(url), null]);
    equal([
      youTubeEmbedUrl(" abcdefghijk "),
      "https://www.youtube.com/embed/abcdefghijk?enablejsapi=1&playsinline=1&controls=0&disablekb=1&fs=0&iv_load_policy=3&cc_load_policy=0&rel=0&modestbranding=1&mute=1"
    ]);
    for (const id of ["bad", "xabcdefghijk", "abcdefghijkx", null, 7]) equal([youTubeEmbedUrl(id), null]);
    deepEqual(
      [createPanoramaPath(() => 0), { xPhaseA: 0, xPhaseB: 0, xPhaseC: 0, yPhaseA: 0, yPhaseB: 0 }],
      [
        createPanoramaPath(() => 0.25),
        {
          xPhaseA: Math.PI / 2,
          xPhaseB: Math.PI / 2,
          xPhaseC: Math.PI / 2,
          yPhaseA: Math.PI / 2,
          yPhaseB: Math.PI / 2
        }
      ]
    );
  });
});
describe("lyrics, melody and pitch", () => {
  test("calculates normalized pitch correlation and lag boundaries", () => {
    equal(
      [normalizedCorrelation(Float32Array.of(1, 2, 3, 4), 1), 20 / Math.sqrt(14 * 29)],
      [normalizedCorrelation(Float32Array.of(1, 0, 0), 1), 0],
      [rootMeanSquare(Float32Array.of(3, 4)), Math.sqrt(12.5)],
      [hasSufficientPitchEnergy([0.01]), true],
      [hasSufficientPitchEnergy([0.009]), false],
      [isConfidentPitchScore(0.62), true],
      [isConfidentPitchScore(0.619), false]
    );
    deepEqual([getPitchLagRange(2048, 48_000), { minLag: 26, maxLag: 874 }]);
    equal([getPitchLagRange(3, 48_000), null]);
    deepEqual([getPitchLagRange(100, 48_000), { minLag: 26, maxLag: 98 }]);
    equal([getPitchLagRange(28, 48_000), null]);
  });
  test("selects the earliest strongest pitch lag and first valid peak", () => {
    const scores = Float64Array.of(0, 0, 0.5, 0.8, 0.7, 0.8, 0.6, 0);
    deepEqual(
      [findBestPitchLag(scores, 2, 6), { lag: 3, score: 0.8 }],
      [findBestPitchLag(new Float64Array(5), 1, 3), { lag: -1, score: 0 }],
      [findBestPitchLag(Float64Array.of(0, 0, 0.5, 0.6, 0.7, 0.8, 0.9), 2, 6), { lag: 6, score: 0.9 }]
    );
    const weak = Float64Array.of(0, 0, 0.6, 0.7, 0.6, 0, 0.6, 0);
    deepEqual([selectFundamentalLag(weak, 1, 7, 7, 1), { lag: 7, score: 1 }]);
    const exact = Float64Array.of(0, 0.9, 0.9, 0.8, 0, 0, 1, 0);
    deepEqual([selectFundamentalLag(exact, 1, 7, 6, 1), { lag: 2, score: 0.9 }]);
    const rightTie = Float64Array.of(0, 0.8, 0.9, 0.9, 0, 0, 1, 0);
    deepEqual([selectFundamentalLag(rightTie, 1, 7, 6, 1), { lag: 3, score: 0.9 }]);
  });
  test("interpolates pitch lag and converts bounded frequencies to MIDI", () => {
    const scores = Float64Array.of(0, 0, 0, 0, 0.8, 1, 0.9, 0, 0);
    assert.ok(Math.abs(refinePitchLag(scores, 5, 2, 8) - 5.1666666667) < 1e-9);
    equal(
      [refinePitchLag(scores, 2, 2, 8), 2],
      [refinePitchLag(scores, 8, 2, 8), 8],
      [refinePitchLag(Float64Array.of(0, 0.5, 1, 0.8), 2, 2, 3), 2],
      [refinePitchLag(Float64Array.of(0, 0, 0, 0, 0, 0, 0, 0.8, 1, 0.5), 8, 2, 8), 8],
      [refinePitchLag(Float64Array.of(0, 1, 1, 1, 0), 2, 0, 4), 2]
    );
    for (const [frequency, midi] of [
      [54, 33],
      [55 * 0.98, 33],
      [55, 33],
      [440, 69],
      [1760, 93],
      [1790, 93],
      [1760 * 1.02, 93]
    ])
      assert.ok(Math.abs(pitchFrequencyToMidi(frequency) - midi) < 1e-9);
    for (const frequency of [NaN, Infinity, 53, 1800]) equal([pitchFrequencyToMidi(frequency), null]);
  });
  test("detects a stable tone and rejects invalid or quiet input", () => {
    const rate = 48_000;
    const length = 2048;
    const detect = (samples, sampleRate = rate) =>
      detectMidiFromAnalyser({ getFloatTimeDomainData: (buffer) => buffer.set(samples) }, new Float32Array(samples.length), sampleRate);
    const sine = (frequency, amplitude = 0.5, envelope = () => 1) => {
      const samples = new Float32Array(length);
      for (let index = 0; index < samples.length; index += 1)
        samples[index] = amplitude * envelope(index) * Math.sin((2 * Math.PI * frequency * index) / rate);
      return samples;
    };
    for (const [frequency, midi] of [
      [55, 33],
      [82.4069, 40],
      [110, 45],
      [220, 57],
      [261.6256, 60],
      [329.6276, 64],
      [440, 69],
      [880, 81],
      [1760, 93]
    ])
      assert.ok(Math.abs(detect(sine(frequency)) - midi) < 0.03, frequency);
    const harmonic = new Float32Array(length);
    for (let index = 0; index < harmonic.length; index += 1)
      harmonic[index] =
        0.35 * Math.sin((2 * Math.PI * 220 * index) / rate) +
        0.2 * Math.sin((2 * Math.PI * 440 * index) / rate) +
        0.1 * Math.sin((2 * Math.PI * 660 * index) / rate);
    assert.ok(Math.abs(detect(harmonic) - 57) < 0.03);
    assert.ok(Math.abs(detect(sine(261.6256, 0.5, (index) => Math.exp(-index / length))) - 60) < 0.03);
    equal([detect(sine(53)), null], [detect(sine(1800)), null], [detect(sine(440, 0.014)), null]);
    assert.ok(Math.abs(detect(sine(440, 0.0142)) - 69) < 0.03);
    const tone = sine(440);
    const analyser = { getFloatTimeDomainData: (buffer) => buffer.set(tone) };
    const unread = vi.fn();
    for (const [buffer, sampleRate] of [
      [null, rate],
      [[], rate],
      [tone, NaN],
      [tone, Infinity],
      [tone, 0],
      [tone, -1]
    ])
      equal([detectMidiFromAnalyser({ getFloatTimeDomainData: unread }, buffer, sampleRate), null]);
    equal([unread.mock.calls.length, 0]);
    for (const [device, buffer, sampleRate] of [
      [null, tone, rate],
      [{}, tone, rate],
      [analyser, null, rate]
    ])
      equal([detectMidiFromAnalyser(device, buffer, sampleRate), null]);
    equal(
      [
        detectMidiFromAnalyser(
          {
            getFloatTimeDomainData: () => {
              throw new Error("device");
            }
          },
          tone,
          rate
        ),
        null
      ],
      [detectMidiFromAnalyser({ getFloatTimeDomainData: (buffer) => buffer.fill(0) }, new Float32Array(length), rate), null],
      [detectMidiFromAnalyser({ getFloatTimeDomainData: (buffer) => buffer.fill(0.5) }, new Float32Array(3), rate), null]
    );
    const impulse = new Float32Array(length);
    impulse[0] = 1;
    equal([detectMidiFromAnalyser({ getFloatTimeDomainData: (buffer) => buffer.set(impulse) }, new Float32Array(length), rate), null]);
    let seed = 1;
    const noise = new Float32Array(length);
    for (let index = 0; index < noise.length; index += 1) {
      seed = (seed * 16_807) % 2_147_483_647;
      noise[index] = (seed / 2_147_483_647 - 0.5) * 0.5;
    }
    equal([detect(noise), null]);
  }, 30_000);
});
describe("device, settings and song-card factories", () => {
  test("deduplicates device and buffer options", () => {
    deepEqual(
      [createIndexedDeviceOptions(null, "Default"), [{ value: "", label: "Default" }]],
      [
        createIndexedDeviceOptions(
          ["invalid", { index: 0, label: "Zero" }, { index: 1, name: "A" }, { index: 1 }, { index: 2 }, null, {}],
          "Default"
        ),
        [
          { value: "", label: "Default" },
          { value: 0, label: "Zero" },
          { value: 1, label: "A" },
          { value: 2, label: translateSaved("Устройство") }
        ]
      ],
      [
        createBrowserDeviceOptions(
          ["invalid", { deviceId: "x", label: "Mic" }, { deviceId: "y" }, { deviceId: "" }],
          "Device",
          "Browser default"
        ),
        [
          { value: "default", label: "Browser default" },
          { value: "x", label: "Mic" },
          { value: "y", label: "Device" }
        ]
      ],
      [createBufferSizeOptions([64, "64", 0, 12.5, "bad"]), [{ value: 64, label: "64 samples" }]],
      [createBufferSizeOptions(null), []],
      [createBufferSizeOptions(), [32, 64, 128, 256, 512].map((value) => ({ value, label: `${value} samples` }))]
    );
    equal(
      [createIndexedDeviceOptions(null)[0].label, translateSaved("По умолчанию")],
      [createBrowserDeviceOptions([], "Device")[0].label, translateSaved("Системное по умолчанию")]
    );
  });
  test("shows only user-facing microphone inputs while preserving an advanced selection", () => {
    const devices = [
      { index: 0, name: "Microsoft Sound Mapper - Input [MME]", host_api: "MME" },
      { index: 1, name: "Analogue 1/2 (Audient) [MME]", host_api: "MME" },
      { index: 2, name: "ADAT 3/4 (Audient) [MME]", host_api: "MME" },
      { index: 13, name: "Analogue 1/2 (Audient) [Windows DirectSound]", host_api: "Windows DirectSound" },
      { index: 30, name: "Analogue 1/2 (Audient) [Windows WASAPI]", host_api: "Windows WASAPI" },
      { index: 31, name: "Loop-back 1/2 (Audient) [Windows WASAPI]", host_api: "Windows WASAPI" },
      { index: 40, name: "Internal kernel input [Windows WDM-KS]", host_api: "Windows WDM-KS" }
    ];

    deepEqual(
      [
        createInputDeviceOptions(devices, null, "Default"),
        [
          { value: "", label: "Default" },
          { value: 30, label: "Analogue 1/2 (Audient)" }
        ]
      ],
      [
        createInputDeviceOptions(devices, 2, "Default"),
        [
          { value: "", label: "Default" },
          { value: 30, label: "Analogue 1/2 (Audient)" },
          { value: 2, label: "ADAT 3/4 (Audient)" }
        ]
      ]
    );
  });
  test("validates and normalizes song settings", () => {
    const songs = [
      { id: "a", title: "A" },
      { id: "b", title: "B" }
    ];
    equal([normalizeText(" x "), "x"]);
    for (const value of [null, undefined, 7, {}, "   "]) equal([normalizeText(value), null]);
    equal(
      [validateSongSettings(undefined), translateSaved("Укажите название песни.")],
      [validateSongSettings({ title: "" }), translateSaved("Укажите название песни.")],
      [validateSongSettings({ title: "x", tempo_override: 0 }), translateSaved("Темп должен быть больше 0 BPM.")],
      [
        validateSongSettings({ title: "x", note_range_min: 90, note_range_max: 20 }),
        translateSaved("Нижняя нота диапазона не может быть выше верхней.")
      ]
    );
    for (const form of [
      { title: "x", tempo_override: 120 },
      { title: "x", tempo_override: "bad" },
      { title: "x", tempo_override: "" },
      { title: "x", tempo_override: null },
      { title: "x", note_range_min: 60, note_range_max: 60 },
      { title: "x", note_range_min: 1 },
      { title: "x", note_range_max: 0 }
    ])
      equal([validateSongSettings(form), null]);
    deepEqual(
      [
        createSongPayload({ title: "", artist: " A ", note_range_min: -2, note_range_max: 130 }, songs[0]),
        {
          title: "A",
          artist: "A",
          genre: null,
          key_override: null,
          tempo_override: null,
          note_range_min: 0,
          note_range_max: 127,
          difficulty_override: null,
          video_url: null
        }
      ],
      [
        createSongPayload(
          {
            title: " New ",
            artist: " ",
            genre: " Rock ",
            key_override: " C# ",
            tempo_override: "128.5",
            note_range_min: "60.4",
            note_range_max: "61.6",
            difficulty_override: " hard ",
            video_url: " https://example.test "
          },
          songs[0]
        ),
        {
          title: "New",
          artist: null,
          genre: "Rock",
          key_override: "C#",
          tempo_override: 128.5,
          note_range_min: 60,
          note_range_max: 62,
          difficulty_override: "hard",
          video_url: "https://example.test"
        }
      ]
    );
    equal(
      [formatSongKey(" A   minor "), "Am"],
      [formatSongKey("C   MAJOR"), "Cmaj"],
      [formatSongKey("A minor extra"), "A minor extra"],
      [formatSongKey("C major extra"), "C major extra"]
    );
    for (const value of [null, undefined, "", "   ", 0]) equal([formatSongKey(value), translateSaved("Тональность определяется")]);
  });
});
