import assert from "node:assert/strict";
import { afterEach, beforeEach, describe, test, vi } from "vitest";
import { deepEqual, equal } from "./helpers/assertions.mjs";

const importApi = async (name) => {
  vi.resetModules();
  return import(`../src/api/${name}.js`).catch(() => {
    throw Error(`Unknown API module: ${name}`);
  });
};
const importDomain = (name) =>
  import(`../src/api/domains/${name}.js`).catch(() => {
    throw Error(`Unknown API domain: ${name}`);
  });
const response = ({ body = "{}", json, ok = true, status = 200, statusText = "", url = "" } = {}) => ({
  ok,
  status,
  statusText,
  url,
  text: async () => body,
  json: async () => json ?? JSON.parse(body),
  blob: async () => new Blob([body])
});
const lastCall = () => fetch.mock.calls.at(-1);
const pathOf = (url) => new URL(url).pathname;
async function assertRequest(invoke, expected) {
  const result = await invoke();
  const [url, { method, keepalive, body, headers }] = lastCall();
  const { pathname, search } = new URL(url);
  deepEqual([
    [pathname + search, method, keepalive],
    [expected.path, expected.method, expected.keepalive]
  ]);
  if ("body" in expected) {
    deepEqual([JSON.parse(body), expected.body]);
    equal([headers["Content-Type"], "application/json"]);
  } else assert.equal(body, undefined);
  return result;
}
const abortPromise = (signal, message) =>
  new Promise((_, reject) =>
    signal.addEventListener(
      "abort",
      () => {
        const error = new Error(message);
        error.name = "AbortError";
        reject(error);
      },
      { once: true }
    )
  );
beforeEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = vi.fn(async () => response());
});
afterEach(() => vi.unstubAllEnvs());
describe("API transport", () => {
  test("uses the development token in a regular browser", async () => {
    vi.stubEnv("VITE_API_TOKEN", "browser-secret");
    const { request } = await importApi("core");

    await request("diagnostics/health");

    equal([lastCall()[1].headers["X-ADVoice-Token"], "browser-secret"]);
  });

  test("normalizes request paths, bodies and successful response kinds", async () => {
    const { request, requestBlob } = await importApi("core");
    fetch
      .mockResolvedValueOnce(response({ body: '{"ok":true}', url: "http://api/value" }))
      .mockResolvedValueOnce(response({ status: 204, body: '{"ignored":true}' }))
      .mockResolvedValueOnce(response({ body: "" }))
      .mockResolvedValueOnce(response({ body: "blob" }));
    deepEqual([await request("value", { method: "POST", body: "{}" }), { ok: true }]);
    let [url, options] = fetch.mock.calls[0];
    equal(
      [pathOf(url), "/value"],
      [options.headers["Content-Type"], "application/json"],
      [await request("/empty"), null],
      [pathOf(fetch.mock.calls[1][0]), "/empty"],
      [await request("/blank"), null],
      [(await requestBlob("blob")).size, 4]
    );
    await request("headers", { headers: new Headers({ Accept: "application/json" }), body: null });
    deepEqual([lastCall()[1].headers, { accept: "application/json" }]);
    await request("array", { headers: [["X-Test", "yes"]], body: new FormData() });
    equal([lastCall()[1].headers["X-Test"], "yes"]);
    await request("form", { body: new FormData() });
    equal([lastCall()[1].headers, undefined]);
    await request("object", { headers: { "Content-Type": "text/plain" }, body: "plain" });
    deepEqual([lastCall()[1].headers, { "Content-Type": "text/plain" }]);
    await request("accept", { headers: { Accept: "application/json" }, body: "{}" });
    deepEqual([lastCall()[1].headers, { Accept: "application/json", "Content-Type": "application/json" }]);
    await request("null", { body: null });
    equal([lastCall()[1].headers, undefined]);
    await request("binary", { body: new Uint8Array([1]) });
    deepEqual([lastCall()[1].headers, {}]);
    const FormDataCtor = globalThis.FormData;
    globalThis.FormData = undefined;
    try {
      await request("without-form-data", { body: "{}" });
    } finally {
      globalThis.FormData = FormDataCtor;
    }
    [url, options] = lastCall();
    equal([pathOf(url), "/without-form-data"], [options.headers["Content-Type"], "application/json"]);
  });
  test("reports HTTP, malformed JSON and missing fetch failures", async () => {
    const { request } = await importApi("core");
    fetch.mockResolvedValueOnce(response({ ok: false, status: 422, json: { detail: "bad" }, url: "http://api/bad" }));
    await assert.rejects(request("bad"), ({ message, status, url }) => message === "bad" && status === 422 && url === "http://api/bad");
    fetch.mockResolvedValueOnce(response({ ok: false, status: 500, statusText: "Failure", json: { detail: { code: 1 } } }));
    await assert.rejects(request("bad"), /\{"code":1\}/);
    fetch.mockResolvedValueOnce({
      ...response({ ok: false, status: 503, statusText: "Offline" }),
      json: async () => {
        throw Error("not json");
      }
    });
    await assert.rejects(request("bad"), ({ message, url }) => message === "Offline" && url === "http://127.0.0.1:8000/bad");
    fetch.mockResolvedValueOnce({
      ...response({ ok: false, status: 418 }),
      json: async () => {
        throw Error("not json");
      }
    });
    await assert.rejects(request("teapot"), /HTTP 418/);
    fetch.mockResolvedValueOnce(response({ body: "not-json", url: "bad-json" }));
    await assert.rejects(request("bad"), /JSON/);
    fetch.mockResolvedValueOnce(response({ ok: false, status: 400, json: { message: "plain" } }));
    await assert.rejects(request(null), /plain/);
    equal([pathOf(lastCall()[0]), "/"]);
    fetch.mockResolvedValueOnce(response({ body: "bad" }));
    await assert.rejects(request("fallback-path"), /fallback-path/);
    delete globalThis.fetch;
    await assert.rejects(request("bad"), /Fetch API/);
  });
  test("aborts stalled backend requests at the configured deadline", async () => {
    vi.useFakeTimers();
    const { request } = await importApi("core");
    fetch.mockImplementationOnce((_, { signal }) => abortPromise(signal, "aborted"));
    const rejection = assert.rejects(request("stalled", { timeoutMs: 25 }), ({ name }) => name === "TimeoutError");
    await vi.advanceTimersByTimeAsync(25);
    await rejection;
    vi.useRealTimers();
  });
  test("keeps the deadline active while the response body is read", async () => {
    vi.useFakeTimers();
    const { request } = await importApi("core");
    fetch.mockImplementationOnce(async (_, { signal }) => ({
      ...response(),
      text: () => abortPromise(signal, "body aborted")
    }));
    const rejection = assert.rejects(request("stalled-body", { timeoutMs: 25 }), ({ name }) => name === "TimeoutError");
    await vi.advanceTimersByTimeAsync(25);
    await rejection;
    vi.useRealTimers();
  });
  test("encodes identifiers and confines local file URLs", async () => {
    const { createFileUrl, encodePathSegment } = await importApi("core");
    equal([encodePathSegment(" a/b "), "a%2Fb"]);
    assert.throws(
      () => encodePathSegment(" "),
      (error) => error instanceof TypeError && error.message.length > 0
    );
    assert.throws(() => encodePathSegment(null), TypeError);
    assert.match(createFileUrl("local/file.wav"), /\/local\/file\.wav$/);
    equal([pathOf(createFileUrl(" /local/file.wav ")), "/local/file.wav"]);
    for (const value of ["", null]) assert.equal(createFileUrl(value), "http://127.0.0.1:8000");
    assert.match(createFileUrl("folder/http:file"), /folder\/http:file$/);
    assert.throws(() => createFileUrl("x1:remote"), TypeError);
    assert.throws(
      () => createFileUrl("https://remote/file"),
      (error) => error instanceof TypeError && error.message.length > 0
    );
  });
  test("routes every transport kind through the optional mock API", async () => {
    vi.stubEnv("VITE_USE_MOCK_API", "true");
    const mockCore = await importApi("core");
    equal([mockCore.MOCK_API_ENABLED, true]);
    fetch.mockClear();
    assert.ok(Array.isArray(await mockCore.request("/songs")));
    assert.ok((await mockCore.requestBlob("/songs/demo/package")).size > 0);
    const url = mockCore.createFileUrl("/audio/file");
    assert.match(url, /^data:audio\/ogg;base64,/);
    assert.ok(url.length > 5_000, "mock audio must be long enough for playback E2E");
    equal([fetch.mock.calls.length, 0]);
  });
});
describe("API normalization", () => {
  test("normalizes primitives and malformed entities", async () => {
    const {
      clampNumber,
      normalizeBoolean,
      normalizeModel,
      normalizeNonNegativeNumber,
      normalizeRecording,
      normalizeSong,
      normalizeSongList,
      normalizeString
    } = await importApi("normalizers");
    equal([clampNumber("3", 0, 2), 2], [clampNumber("x", 0, 2, 1), 1]);
    for (const value of [null, "  "]) assert.equal(normalizeString(value, "fallback"), "fallback");
    for (const value of [true, 1, "YES", "on"]) assert.equal(normalizeBoolean(value), true);
    for (const value of [false, 0, "false", "0", "NO", "off", ""]) equal([normalizeBoolean(value, true), false]);
    equal([normalizeBoolean(" true "), true]);
    for (const value of [NaN, "maybe", {}]) assert.equal(normalizeBoolean(value, true), true);
    deepEqual(
      [
        [normalizeNonNegativeNumber(-1, 4), normalizeNonNegativeNumber(0, 4), normalizeNonNegativeNumber("2")],
        [4, 0, 2]
      ],
      [normalizeSongList(null), []]
    );
    equal(
      [normalizeSong({ status: "UNKNOWN", progress_percent: 999 }).status, "pending"],
      [normalizeSong({ status: "DONE", error_message: " " }).error_message, null]
    );
    deepEqual([normalizeSongList([null]).map(({ status }) => status), ["pending"]]);
    for (const status of ["pending", "queued", "processing", "cancelling", "cancelled", "done", "error"])
      equal([normalizeSong({ status }).status, status]);
    equal([Object.hasOwn(normalizeSong("x"), "0"), false]);
    deepEqual([normalizeModel(null), { name: "", downloaded: false, selected: false, size: 0 }]);
    equal([Object.hasOwn(normalizeModel("x"), "0"), false], [normalizeRecording({ duration_sec: -2 }).duration_sec, 0]);
    deepEqual([normalizeRecording(null), { id: "", song_id: "", duration_sec: 0 }]);
    equal([Object.hasOwn(normalizeRecording("x"), "0"), false]);
  });
});
describe("API domains", () => {
  test("routes audio, player and system operations", async () => {
    const [{ audioApi }, { playerApi }, { systemApi }] = await Promise.all([
      importDomain("audio"),
      importDomain("player"),
      importDomain("system")
    ]);
    const operations = [
      [audioApi.listAudioDevices, "/audio/devices"],
      [audioApi.listAudioOutputDevices, "/audio/output-devices"],
      [audioApi.listAsioDrivers, "/audio/asio-drivers"],
      [audioApi.getAudioSettings, "/audio/settings"],
      [() => audioApi.updateAudioSettings({ latency: 1 }), "/audio/settings", "POST", { latency: 1 }],
      [audioApi.startDirectMonitoring, "/audio/direct-monitor/start?disabled_effects=false", "POST"],
      [() => audioApi.setDirectMonitorMediaActive(true), "/audio/direct-monitor/media-active?active=true", "POST"],
      [audioApi.stopDirectMonitoring, "/audio/direct-monitor/stop", "POST"],
      [audioApi.getSignalQuality, "/audio/signal-quality"],
      [() => playerApi.getSync("a/b"), "/player/a%2Fb/sync"],
      [() => playerApi.getTimeline("a/b"), "/player/a%2Fb/timeline"],
      [() => playerApi.getPosition("a/b"), "/player/a%2Fb/position"],
      [() => playerApi.seek("a/b", 2.5), "/player/a%2Fb/seek", "POST", { position_sec: 2.5 }],
      [() => playerApi.play("a/b"), "/player/a%2Fb/resume", "POST"],
      [() => playerApi.pause("a/b"), "/player/a%2Fb/pause", "POST"],
      [() => playerApi.stop("a/b"), "/player/a%2Fb/stop", "POST"],
      [systemApi.getCacheSize, "/cache/size"],
      [systemApi.getFreeSpace, "/cache/free-space"],
      [systemApi.clearCache, "/cache/clear", "POST"],
      [systemApi.deleteTemp, "/cache/temp", "DELETE"],
      [() => systemApi.optimizeSong("a/b"), "/cache/optimize/a%2Fb", "POST"],
      [systemApi.getHealth, "/diagnostics/health"],
      [systemApi.getPipelineHealth, "/diagnostics/pipeline"],
      [systemApi.getAiModelsStatus, "/diagnostics/ai-models"],
      [systemApi.downloadAiModels, "/diagnostics/ai-models/download", "POST"],
      [systemApi.getVersions, "/diagnostics/versions"],
      [systemApi.getErrors, "/diagnostics/errors"],
      [systemApi.getHistory, "/history"],
      [systemApi.getAbout, "/about"]
    ];
    for (const [invoke, path, method, body] of operations)
      await assertRequest(invoke, { path, method, ...(body === undefined ? {} : { body }) });
    await assertRequest(audioApi.releaseDirectMonitoring, {
      path: "/audio/direct-monitor/stop",
      method: "POST",
      keepalive: true
    });
    await assertRequest(() => audioApi.startDirectMonitoring({ disabledEffects: true, wasapiMode: "shared", autoBuffer: true }), {
      path: "/audio/direct-monitor/start?disabled_effects=true",
      method: "POST"
    });
    fetch.mockRejectedValueOnce(Error("closed"));
    equal([await audioApi.releaseDirectMonitoring(), null]);
  });
  test("allows the backend monitor worker to finish its Windows driver fallback", async () => {
    vi.useFakeTimers();
    try {
      const { audioApi } = await importDomain("audio");
      fetch.mockImplementationOnce((_, { signal }) => abortPromise(signal, "aborted"));
      const requestPromise = audioApi.startDirectMonitoring();
      let settled = false;
      requestPromise
        .finally(() => {
          settled = true;
        })
        .catch(() => {});
      const rejection = assert.rejects(requestPromise, ({ name }) => name === "TimeoutError");

      await vi.advanceTimersByTimeAsync(15_000);
      assert.equal(settled, false);
      await vi.advanceTimersByTimeAsync(10_000);
      await rejection;
    } finally {
      vi.useRealTimers();
    }
  });
  test("routes recording operations and normalizes collections", async () => {
    const { recordingsApi } = await importDomain("recordings");
    for (const [args, body] of [
      [
        ["song"],
        {
          song_id: "song",
          position_sec: 0,
          music_volume: 1,
          microphone_volume: 1,
          reverb: 0,
          echo: 0,
          delay: 0,
          octave: 0,
          playback_rate: 1,
          room_mode: false,
          voice_relay: false
        }
      ],
      [
        ["song", 1, 0.8, 0.7, 0.1, 0.2, 0.3, true, 0, 1.25, true],
        {
          song_id: "song",
          position_sec: 1,
          music_volume: 0.8,
          microphone_volume: 0.7,
          reverb: 0.1,
          echo: 0.2,
          delay: 0.3,
          octave: 0,
          playback_rate: 1.25,
          room_mode: true,
          voice_relay: true
        }
      ]
    ])
      await assertRequest(() => recordingsApi.startRecording(...args), {
        path: "/recording/start",
        method: "POST",
        body
      });
    for (const [invoke, path] of [
      [() => recordingsApi.pauseRecording("a/b ?"), "/recording/pause?session_id=a%2Fb%20%3F"],
      [() => recordingsApi.pauseRecording(null), "/recording/pause?session_id="],
      [() => recordingsApi.resumeRecording("a/b ?"), "/recording/resume?session_id=a%2Fb%20%3F"],
      [() => recordingsApi.resumeRecording(null), "/recording/resume?session_id="],
      [() => recordingsApi.syncRecording("a/b ?", 4.25, 1.25), "/recording/sync?session_id=a%2Fb%20%3F&position_sec=4.25&playback_rate=1.25"],
      [() => recordingsApi.stopRecording("a/b ?"), "/recording/stop?session_id=a%2Fb%20%3F"],
      [() => recordingsApi.stopRecording(null), "/recording/stop?session_id="]
    ])
      await assertRequest(invoke, { path, method: "POST" });
    await assertRequest(
      () =>
        recordingsApi.updateRecordingControls("a/b ?", {
          musicVolume: 0.25,
          microphoneVolume: 1.75,
          reverb: 0.4,
          echo: 0.3,
          delay: 0.2,
          octave: -0.5
        }),
      {
        path: "/recording/controls?session_id=a%2Fb%20%3F",
        method: "PATCH",
        body: {
          music_volume: 0.25,
          microphone_volume: 1.75,
          reverb: 0.4,
          echo: 0.3,
          delay: 0.2,
          octave: -0.5
        }
      }
    );
    fetch.mockResolvedValueOnce(response({ body: '[{"id":1,"duration_sec":"2"}]' }));
    equal([(await recordingsApi.listRecordingsForSong("song"))[0].duration_sec, 2], [pathOf(lastCall()[0]), "/recording/by-song/song"]);
    fetch.mockResolvedValueOnce(response({ body: "null" }));
    deepEqual([await recordingsApi.listRecordingsForSong("song"), []]);
    equal([pathOf(lastCall()[0]), "/recording/by-song/song"]);
    fetch.mockResolvedValueOnce(response({ body: "null" }));
    deepEqual([await recordingsApi.listRecordingLibrary(), []]);
    equal([pathOf(lastCall()[0]), "/recording/library"]);
    fetch.mockResolvedValueOnce(response({ body: '[{"id":"library"}]' }));
    equal([(await recordingsApi.listRecordingLibrary())[0].id, "library"], [pathOf(lastCall()[0]), "/recording/library"]);
    await assertRequest(recordingsApi.getRecordingSettings, { path: "/recording/settings" });
    await assertRequest(() => recordingsApi.deleteRecording("a/b"), {
      path: "/recording/a%2Fb",
      method: "DELETE"
    });
    equal(
      [pathOf(recordingsApi.getRecordingFileUrl("a/b")), "/recording/a%2Fb/file"],
      [pathOf(recordingsApi.getPerformanceFileUrl("a/b")), "/recording/a%2Fb/performance"]
    );
    await assertRequest(() => recordingsApi.runAnalysis("a/b"), {
      path: "/analysis/a%2Fb/run",
      method: "POST"
    });
    await assertRequest(() => recordingsApi.getAnalysis("a/b"), { path: "/analysis/a%2Fb" });
  });
  test("routes song reads and normalizes their public results", async () => {
    const { songsApi } = await importDomain("songs");
    fetch
      .mockResolvedValueOnce(response({ body: '[{"id":1,"title":" Song ","status":"DONE","progress_percent":120}]' }))
      .mockResolvedValueOnce(response({ body: '{"id":"a/b","title":" ","status":"UNKNOWN","progress_percent":-1}' }));
    deepEqual([
      await songsApi.listSongs(),
      [
        {
          id: "1",
          title: "Song",
          status: "done",
          progress_percent: 100,
          progress_step: "",
          error_message: null
        }
      ]
    ]);
    equal([pathOf(fetch.mock.calls[0][0]), "/songs"]);
    deepEqual([
      await songsApi.getSong(" a/b "),
      {
        id: "a/b",
        title: "Без названия",
        status: "pending",
        progress_percent: 0,
        progress_step: "",
        error_message: null
      }
    ]);
    equal([pathOf(fetch.mock.calls[1][0]), "/songs/a%2Fb"]);
  });
  test("routes song mutations with exact methods and JSON contracts", async () => {
    const { songsApi } = await importDomain("songs");
    const id = "a/b";
    for (const [invoke, path, method, body] of [
      [() => songsApi.updateSong(id, { title: "x" }), "/songs/a%2Fb", "PATCH", { title: "x" }],
      [() => songsApi.deleteSong(id), "/songs/a%2Fb", "DELETE"],
      [() => songsApi.processSong(id), "/songs/a%2Fb/process", "POST", { mode: "auto" }],
      [() => songsApi.reprocessMelody(id), "/songs/a%2Fb/reprocess", "POST"],
      [() => songsApi.cancelProcessing(id), "/songs/a%2Fb/cancel", "POST"],
      [() => songsApi.getStatus(id), "/songs/a%2Fb/status"],
      [() => songsApi.getLog(id), "/songs/a%2Fb/log"],
      [() => songsApi.getResult(id), "/songs/a%2Fb/result"],
      [() => songsApi.resolveSongRevision("sha256:abc"), "/songs/revision/resolve", "POST", { revision: "sha256:abc" }],
      [() => songsApi.getSongEditor(id), "/songs/a%2Fb/editor"],
      [
        () => songsApi.saveSongEditor(id, [{ pitch: 60 }]),
        "/songs/a%2Fb/editor",
        "PUT",
        { notes: [{ pitch: 60 }], word_texts: null, word_bounds: null }
      ],
      [() => songsApi.resetSongEditor(id), "/songs/a%2Fb/editor/reset", "POST"],
      [() => songsApi.updateLyrics(id, "lyrics"), "/songs/a%2Fb/lyrics", "PUT", { lyrics: "lyrics" }]
    ])
      await assertRequest(invoke, {
        path,
        method,
        ...(body === undefined ? {} : { body })
      });
  });
  test("uploads, exports and creates audio URLs with exact song package contracts", async () => {
    const { songsApi } = await importDomain("songs");
    const song = new Blob(["song"]);
    fetch.mockResolvedValueOnce(response({ body: '{"title":"Tagged","artist":"Artist"}' }));
    deepEqual([await songsApi.inspectSongIdentity(song), { title: "Tagged", artist: "Artist" }]);
    let [url, options] = lastCall();
    deepEqual([
      [pathOf(url), options.method, await options.body.get("file").text()],
      ["/songs/identity", "POST", "song"]
    ]);
    await songsApi.addSong(song, "Title");
    [url, options] = lastCall();
    deepEqual([
      [pathOf(url), options.method, await options.body.get("file").text(), options.body.get("title")],
      ["/songs", "POST", "song", "Title"]
    ]);
    await songsApi.addSong(song);
    [, options] = lastCall();
    deepEqual([[...options.body.keys()], ["file", "artist"]]);
    await songsApi.prepareKarDataset([new Blob(["kar-one"]), new Blob(["kar-two"])]);
    [url, options] = lastCall();
    deepEqual([
      [pathOf(url), options.method, await Promise.all(options.body.getAll("files").map((file) => file.text()))],
      ["/songs/training/kar", "POST", ["kar-one", "kar-two"]]
    ]);
    equal([pathOf(songsApi.getAudioTrackUrl("a/b", "lead vocal")), "/songs/a%2Fb/audio/lead%20vocal"]);
    const coverUrl = new URL(songsApi.getSongCoverUrl("a/b", "2026-08-21 15:00"));
    deepEqual([
      [coverUrl.pathname, coverUrl.searchParams.get("v")],
      ["/songs/a%2Fb/cover", "2026-08-21 15:00"]
    ]);
    fetch.mockResolvedValueOnce(response({ body: "package" }));
    equal([await (await songsApi.exportSongPackage("a/b", "sha256:abc")).text(), "package"]);
    [url, options] = lastCall();
    deepEqual([
      [pathOf(url), new URL(url).searchParams.get("expected_revision"), options.method],
      ["/songs/a%2Fb/package", "sha256:abc", undefined]
    ]);
    const archive = new Blob(["zip"]);
    await songsApi.importSongPackage(archive);
    [url, options] = lastCall();
    deepEqual([
      [pathOf(url), options.method, options.body.get("file").name, await options.body.get("file").text()],
      ["/songs/package/import", "POST", "song.karaoke.zip", "zip"]
    ]);
    await songsApi.importSongPackage(archive, "custom.zip", { expectedRevision: "sha256:def" });
    [url, options] = lastCall();
    deepEqual([
      [options.body.get("file").name, new URL(url).searchParams.get("expected_revision")],
      ["custom.zip", "sha256:def"]
    ]);
  });
  test("persists backend settings and UI preferences", async () => {
    const { settingsApi } = await importDomain("settings");
    globalThis.localStorage = { getItem: () => "dark", setItem: vi.fn() };
    globalThis.window = { localStorage };
    fetch.mockResolvedValueOnce(response());
    equal([(await settingsApi.getAppSettings()).theme, "dark"], [pathOf(fetch.mock.calls[0][0]), "/settings"]);
    fetch.mockResolvedValue(response({ body: '{"ok":true}' }));
    const updated = await settingsApi.updateAppSettings({ theme: "light", audio: true });
    deepEqual([updated, { ok: true, theme: "light" }], [localStorage.setItem.mock.calls[0], ["karaoke-theme", "light"]]);
    const [, options] = lastCall();
    equal([options.method, "PATCH"]);
    deepEqual([JSON.parse(options.body), { audio: true, theme: "light" }]);
    await assertRequest(() => settingsApi.updateAppSettings(null), {
      path: "/settings",
      method: "PATCH",
      body: {}
    });
    await assertRequest(settingsApi.getRemoteDiagnosticsPolicy, {
      path: "/settings/remote-diagnostics"
    });
    await assertRequest(settingsApi.deleteRemoteDiagnostics, {
      path: "/settings/remote-diagnostics",
      method: "DELETE"
    });
    await assertRequest(settingsApi.getUiPreferences, { path: "/preferences" });
    await assertRequest(() => settingsApi.updateUiPreferences("karaoke room", { radio: true }), {
      path: "/preferences/karaoke%20room",
      method: "PATCH",
      body: { radio: true }
    });
    let releaseFirst;
    fetch
      .mockReturnValueOnce(
        new Promise((resolve) => {
          releaseFirst = () => resolve(response());
        })
      )
      .mockResolvedValueOnce(response());
    const first = settingsApi.updateUiPreferences("audio", { device: "old" });
    const second = settingsApi.updateUiPreferences("audio", { device: "new" });
    await Promise.resolve();
    equal([fetch.mock.calls.filter(([url]) => pathOf(url) === "/preferences/audio").length, 1]);
    releaseFirst();
    await Promise.all([first, second]);
    deepEqual([
      fetch.mock.calls.filter(([url]) => pathOf(url) === "/preferences/audio").map(([, { body }]) => JSON.parse(body)),
      [{ device: "old" }, { device: "new" }]
    ]);
  });
});
