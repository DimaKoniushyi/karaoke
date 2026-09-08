/* @vitest-environment jsdom */
import { createRef, StrictMode } from "react";
import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { verify } from "./helpers/assertions.mjs";
const apiMocks = vi.hoisted(() => ({ getAudioTrackBlob: vi.fn() }));
vi.mock("../src/api/client", () => ({
  api: {
    getAudioTrackUrl: (id, track) => `${id}/${track}`,
    getAudioTrackBlob: apiMocks.getAudioTrackBlob,
    getSongCoverUrl: (id) => `/songs/${id}/cover`,
    getSongVideoUrl: (id) => `/songs/${id}/video`
  }
}));
import KaraokeMedia from "../src/pages/Karaoke/media.jsx";
import SongStrip from "../src/pages/Karaoke/console/song-strip.jsx";
import KaraokeLyrics from "../src/pages/Karaoke/performance-stage/karaoke-lyrics/index.jsx";
const KaraokeLyricLine = ({ line, currentTime }) => (
  <KaraokeLyrics lyricsSync={{ text: line.text, words: line.words }} currentTime={currentTime} />
);
beforeEach(() => {
  apiMocks.getAudioTrackBlob.mockReset();
  apiMocks.getAudioTrackBlob.mockImplementation((_id, track) => Promise.resolve(new Blob([track])));
  vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
  delete globalThis.electronAPI;
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
test("karaoke media preserves direct Electron URLs through StrictMode cleanup", () => {
  globalThis.electronAPI = { isElectron: true };
  const props = {
    instrumentalRef: createRef(),
    vocalsRef: createRef(),
    videoRef: createRef(),
    youTubeClipRef: createRef(),
    isPlaying: false,
    musicVolume: 0.5,
    vocalVolume: 0.4,
    speed: 1,
    youTubeVideoId: "",
    sendYouTubeCommand: vi.fn(),
    syncSecondaryMedia: vi.fn()
  };
  const view = (song) => (
    <StrictMode>
      <KaraokeMedia {...props} song={song} />
    </StrictMode>
  );
  const { container, rerender, unmount } = render(view({ id: "electron-song", title: "Title" }));
  expect([...container.querySelectorAll("audio")].map((audio) => audio.getAttribute("src"))).toEqual([
    "electron-song/instrumental",
    "electron-song/vocals"
  ]);
  rerender(view({ id: "next-song", title: "Next" }));
  expect([...container.querySelectorAll("audio")].map((audio) => audio.getAttribute("src"))).toEqual([
    "next-song/instrumental",
    "next-song/vocals"
  ]);
  expect(apiMocks.getAudioTrackBlob).not.toHaveBeenCalled();
  unmount();
  expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
  expect(HTMLMediaElement.prototype.load).not.toHaveBeenCalled();
});
test("karaoke media releases authenticated files resolved after unmount", async () => {
  const resolvers = [];
  const release = vi.fn();
  const createObjectURL = vi.spyOn(URL, "createObjectURL");
  const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL");
  apiMocks.getAudioTrackBlob.mockImplementation(() => new Promise((resolve) => resolvers.push(resolve)));
  const view = render(
    <KaraokeMedia
      instrumentalRef={createRef()}
      vocalsRef={createRef()}
      videoRef={createRef()}
      youTubeClipRef={createRef()}
      isPlaying={false}
      musicVolume={0.5}
      vocalVolume={0.4}
      speed={1}
      song={{ id: "late-song", title: "Title" }}
      youTubeVideoId=""
      sendYouTubeCommand={vi.fn()}
      syncSecondaryMedia={vi.fn()}
    />
  );
  view.unmount();
  await act(async () => {
    resolvers.forEach((resolve) => {
      const file = new Blob(["audio"]);
      file.cleanup = release;
      resolve(file);
    });
    await Promise.resolve();
  });
  expect(createObjectURL).not.toHaveBeenCalled();
  expect(revokeObjectURL).not.toHaveBeenCalled();
  expect(release).toHaveBeenCalledTimes(2);
});
test("karaoke media leaves failed browser tracks unloaded", async () => {
  apiMocks.getAudioTrackBlob.mockRejectedValue(new Error("forbidden"));
  const { container } = render(
    <KaraokeMedia
      instrumentalRef={createRef()}
      vocalsRef={createRef()}
      videoRef={createRef()}
      youTubeClipRef={createRef()}
      isPlaying={false}
      musicVolume={0.5}
      vocalVolume={0.4}
      speed={1}
      song={{ id: "failed-song", title: "Title" }}
      youTubeVideoId=""
      sendYouTubeCommand={vi.fn()}
      syncSecondaryMedia={vi.fn()}
    />
  );
  await waitFor(() => expect(apiMocks.getAudioTrackBlob).toHaveBeenCalledTimes(2));
  expect([...container.querySelectorAll("audio")].every((audio) => !audio.hasAttribute("src"))).toBe(true);
});
test("karaoke media loads authenticated audio blobs and activates a verified local clip", async () => {
  const createObjectURL = vi.spyOn(URL, "createObjectURL").mockImplementation((blob) => `blob:${blob.size}`);
  const revokeObjectURL = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {});
  const sync = vi.fn();
  const availability = vi.fn();
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
  const instrumentalRef = createRef();
  const { container, rerender } = render(
    <KaraokeMedia
      instrumentalRef={instrumentalRef}
      vocalsRef={createRef()}
      videoRef={createRef()}
      youTubeClipRef={createRef()}
      isPlaying
      musicVolume={0.5}
      vocalVolume={0.4}
      speed={1.25}
      song={{ id: "song", title: "Title", video_url: "local:clip" }}
      syncSecondaryMedia={sync}
      onClipAvailabilityChange={availability}
    />
  );
  expect([...container.querySelectorAll("audio")].every((audio) => !audio.hasAttribute("src"))).toBe(true);
  await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(2));
  const audio = container.querySelector("audio");
  expect(audio.getAttribute("src")).toBe("blob:12");
  expect(audio.volume).toBeGreaterThan(0);
  const video = container.querySelector("video");
  verify([video.getAttribute("src"), "toBe", "/songs/song/video"]);
  expect(container.querySelector("iframe")).toBeNull();
  fireEvent.loadedData(video);
  expect(sync).toHaveBeenCalled();
  expect(availability).toHaveBeenLastCalledWith(true);
  expect(video.play).toHaveBeenCalled();
  rerender(
    <KaraokeMedia
      instrumentalRef={instrumentalRef}
      vocalsRef={createRef()}
      videoRef={createRef()}
      isPlaying={false}
      musicVolume={0.5}
      vocalVolume={0.4}
      speed={1}
      song={{ id: "song", title: "Title" }}
      syncSecondaryMedia={sync}
    />
  );
  expect(container.querySelector("video")).toBeNull();
  cleanup();
  expect(revokeObjectURL).toHaveBeenCalledTimes(2);
});
test("karaoke media ignores legacy YouTube URLs and keeps the default video fallback", async () => {
  const availability = vi.fn();
  const { container } = render(
    <KaraokeMedia
      instrumentalRef={createRef()}
      vocalsRef={createRef()}
      videoRef={createRef()}
      isPlaying={false}
      musicVolume={0.5}
      vocalVolume={0.4}
      speed={1}
      song={{ id: "blocked-song", title: "Title", video_url: "https://youtube.com/watch?v=abcdefghijk" }}
      syncSecondaryMedia={vi.fn()}
      onClipAvailabilityChange={availability}
    />
  );
  expect(container.querySelector("iframe")).toBeNull();
  expect(container.querySelector("video")).toBeNull();
  expect(availability).toHaveBeenLastCalledWith(false);
});
test("waveform supports click, drag and range seeking", () => {
  const change = vi.fn();
  const song = { id: "song", title: "Track" };
  const renderStrip = (currentTime, duration) => (
    <SongStrip song={song} currentTime={currentTime} duration={duration} onSeek={change} disablelabel />
  );
  const { container, rerender } = render(renderStrip(2, 10));
  const timeline = container.querySelector('[data-role="waveform"]');
  timeline.getBoundingClientRect = () => ({ left: 10, width: 100 });
  fireEvent.pointerDown(timeline, { clientX: 60 });
  fireEvent.pointerMove(timeline, { clientX: 80, buttons: 1 });
  fireEvent.pointerMove(timeline, { clientX: 90, buttons: 0 });
  fireEvent.change(container.querySelector("input"), { target: { value: "7" } });
  expect(change).toHaveBeenCalledTimes(3);
  rerender(renderStrip(0, 0));
  fireEvent.pointerDown(container.querySelector('[data-role="waveform"]'), { clientX: 10 });
  expect(change).toHaveBeenCalledTimes(3);
  rerender(renderStrip(0, 10));
  const zeroWidth = container.querySelector('[data-role="waveform"]');
  zeroWidth.getBoundingClientRect = () => ({ left: 0, width: 0 });
  fireEvent.pointerDown(zeroWidth, { clientX: 0 });
  expect(change).toHaveBeenCalledTimes(3);
});
test("lyrics highlight every word only between its exact lyricsSync start and end", () => {
  const { container, rerender } = render(
    <KaraokeLyricLine
      currentTime={3.888836032388664}
      className="line"
      line={{
        text: "Я не",
        words: [
          {
            index: 0,
            text: "Я",
            start: 3.888836032388664,
            end: 3.9088815789473683
          },
          {
            index: 1,
            text: "не",
            start: 4.069245951417004,
            end: 4.129382591093117,
            syllables: [{ text: "ignored", start: 0, end: 10 }]
          }
        ]
      }}
    />
  );
  expect([...container.querySelectorAll('[data-role="lyric-word"]')].map(({ dataset }) => dataset.text).join("")).toBe("Яне");
  expect(container.querySelectorAll('[data-role="lyric-syllable"]')).toHaveLength(0);
  expect(container.querySelectorAll('[data-role="lyric-word"]')[0].style.getPropertyValue("--character-fill")).toBe("0%");
  rerender(
    <KaraokeLyricLine
      currentTime={3.9088815789473683}
      line={{
        text: "Я",
        words: [{ text: "Я", start: 3.888836032388664, end: 3.9088815789473683 }]
      }}
    />
  );
  expect(container.querySelector('[data-role="lyric-word"]').style.getPropertyValue("--character-fill")).toBe("100%");
  rerender(
    <KaraokeLyricLine
      currentTime={(4.790885627530364 + 4.83475) / 2}
      line={{
        text: "любви",
        words: [{ text: "любви", start: 4.790885627530364, end: 4.83475 }]
      }}
    />
  );
  expect(Number.parseFloat(container.querySelector('[data-role="lyric-word"]').style.getPropertyValue("--character-fill"))).toBeCloseTo(
    50,
    10
  );
});

test("lyrics keep a readable constant pace across the exact acoustic note envelope", () => {
  const word = {
    index: 0,
    text: "тяну",
    start: 0,
    end: 2,
    notes: [
      { note: 60, start: 0.2, end: 0.7 },
      { note: 62, start: 1.2, end: 1.7 }
    ]
  };
  const { container, rerender } = render(<KaraokeLyrics lyricsSync={{ text: "тяну", words: [word] }} currentTime={0.5} />);
  const fill = () => Number.parseFloat(container.querySelector('[data-role="lyric-word"]').style.getPropertyValue("--character-fill"));
  const renderedWord = container.querySelector('[data-role="lyric-word"]');
  expect(renderedWord.style.background).toBe("");
  expect(renderedWord.style.filter).toBe("");
  expect(renderedWord.querySelector('[data-role="lyric-word-fill"]').style.clipPath).toContain("--character-fill");
  expect(fill()).toBeCloseTo(20, 10);
  rerender(<KaraokeLyrics lyricsSync={{ text: "тяну", words: [word] }} currentTime={0.9} />);
  expect(fill()).toBeCloseTo(46.6666667, 6);
  rerender(<KaraokeLyrics lyricsSync={{ text: "тяну", words: [word] }} currentTime={1.45} />);
  expect(fill()).toBeCloseTo(83.3333333, 6);
  rerender(<KaraokeLyrics lyricsSync={{ text: "тяну", words: [word] }} currentTime={1.7} />);
  expect(fill()).toBe(100);
  rerender(<KaraokeLyrics lyricsSync={{ text: "тяну", words: [word] }} currentTime={2} />);
  expect(fill()).toBe(100);
});

test("KAR lyrics fill every preserved syllable on its own timing", () => {
  const word = {
    index: 0,
    text: "привет",
    start: 0,
    end: 1,
    notes: [{ note: 60, start: 0, end: 1 }],
    syllables: [
      { text: "при", start: 0, end: 0.2 },
      { text: "вет", start: 0.2, end: 1 }
    ]
  };
  const view = render(<KaraokeLyrics lyricsSync={{ text: "привет", words: [word] }} currentTime={0.1} />);
  const fills = () =>
    [...view.container.querySelectorAll('[data-role="lyric-syllable"]')].map((node) =>
      Number.parseFloat(node.style.getPropertyValue("--character-fill"))
    );

  expect(view.container.querySelectorAll('[data-role="lyric-syllable"]')).toHaveLength(2);
  expect(view.container.querySelector('[data-role="lyric-word-fill"]')).toBeNull();
  expect(fills()).toEqual([50, 0]);

  view.rerender(<KaraokeLyrics lyricsSync={{ text: "привет", words: [word] }} currentTime={0.6} />);
  expect(fills()[0]).toBe(100);
  expect(fills()[1]).toBeCloseTo(50, 10);
});

test("lyrics show the current line and the remaining preview lines", () => {
  const lyricsSync = {
    text: "Первая строка\n.\nВторая строка\nТретья строка",
    words: [
      { index: 0, text: "Первая", start: 1.01, end: 1.41 },
      { index: 1, text: "строка", start: 1.42, end: 1.82 },
      { index: 2, text: "Вторая", start: 2.01, end: 2.41 },
      { index: 3, text: "строка", start: 2.42, end: 2.82 },
      { index: 4, text: "Третья", start: 3.01, end: 3.41 },
      { index: 5, text: "строка", start: 3.42, end: 3.82 }
    ]
  };
  const { container, rerender } = render(<KaraokeLyrics lyricsSync={lyricsSync} currentTime={1.5} />);

  let lines = container.querySelectorAll('[data-role="lyric-line"]');
  expect(lines).toHaveLength(3);
  const lineText = (line) => [...line.querySelectorAll(':scope > [data-role="lyric-word"]')].map(({ dataset }) => dataset.text).join("");
  expect(lineText(lines[0])).toBe("Перваястрока");
  expect(lineText(lines[1])).toBe("Втораястрока");
  expect(lineText(lines[2])).toBe("Третьястрока");
  expect(lines[0].querySelector("[data-start]").dataset.start).toBe("1.01");
  expect(lines[0].querySelector("[data-end]").dataset.end).toBe("1.41");

  rerender(<KaraokeLyrics lyricsSync={lyricsSync} currentTime={2.5} />);
  lines = container.querySelectorAll('[data-role="lyric-line"]');
  expect(lines).toHaveLength(2);
  expect(lineText(lines[0])).toBe("Втораястрока");
  expect(lineText(lines[1])).toBe("Третьястрока");
  expect(container.textContent).not.toContain("Первая");
});

test("standalone KAR punctuation stays on its source line without shifting following lyrics", () => {
  const lyricsSync = {
    text: "Ты - летящий вдаль\nСледующая строка",
    words: [
      { index: 0, text: "Ты", start: 1, end: 1.2 },
      { index: 1, text: "-", start: 1.2, end: 1.3 },
      { index: 2, text: "летящий", start: 1.3, end: 1.8 },
      { index: 3, text: "вдаль", start: 1.8, end: 2.4 },
      { index: 4, text: "Следующая", start: 3, end: 3.5 },
      { index: 5, text: "строка", start: 3.5, end: 4 }
    ]
  };
  const { container } = render(<KaraokeLyrics lyricsSync={lyricsSync} currentTime={1.5} />);
  const lines = container.querySelectorAll('[data-role="lyric-line"]');
  const texts = [...lines].map((line) =>
    [...line.querySelectorAll(':scope > [data-role="lyric-word"]')].map(({ dataset }) => dataset.text).join(" ")
  );

  expect(texts).toEqual(["Ты - летящий вдаль", "Следующая строка"]);
});
