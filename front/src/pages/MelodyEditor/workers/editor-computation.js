import { flattenLyricsNotes } from "../../../utils/lyrics-sync";
import { normalizeNotes, recombineAdjacentEqualPitchNotes } from "../model.js";

export const prepareEditorNotes = (lyricsSync) =>
  normalizeNotes(recombineAdjacentEqualPitchNotes(flattenLyricsNotes(lyricsSync)));
