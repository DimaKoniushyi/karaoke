import { wrap } from "comlink";
import { prepareEditorNotes as prepareLocally } from "./editor-computation";

let remote;
const getRemote = () => {
  if (typeof Worker !== "function") return null;
  remote ??= wrap(new Worker(new URL("./editor.worker.js", import.meta.url), { type: "module" }));
  return remote;
};

export const prepareEditorNotes = (lyricsSync) =>
  getRemote()?.prepareEditorNotes(lyricsSync) ?? Promise.resolve(prepareLocally(lyricsSync));
