import { useCallback, useEffect, useReducer, useState } from "react";
import { api } from "../../api/client";
import { translateSaved as t } from "../../i18n/runtime";
import { getErrorMessage } from "../../utils/errors";
import { documentReducer, initialDocument, serializeNotes, serializeWordBounds } from "./model";
import { prepareEditorNotes } from "./workers/editor-client";

export default function useEditorDocument({ songId, confirm, notify }) {
  const [song, setSong] = useState(null);
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [document, dispatch] = useReducer(documentReducer, initialDocument);

  useEffect(() => {
    if (!songId) return undefined;
    let active = true;
    api
      .listSongs()
      .then((songs) => {
        if (!active) return;
        setSong(
          (songs || []).find(({ id }) => String(id) === String(songId)) || {
            id: songId,
            title: t("editor.melodyEditor2")
          }
        );
      })
      .catch(() => active && setSong({ id: songId, title: t("editor.melodyEditor2") }));
    return () => {
      active = false;
    };
  }, [songId]);

  const accept = useCallback(async (result) => {
    const notes = await prepareEditorNotes(result?.lyrics_sync);
    setPayload(result);
    const words = result?.lyrics_sync?.words || [];
    const wordTexts = words.map((word) => String(word.text || ""));
    const wordBounds = words.map((word) => ({ start: Number(word.start), end: Number(word.end) }));
    dispatch({ type: "load", notes, wordTexts, wordBounds });
  }, []);

  const load = useCallback(async () => {
    if (!song?.id) return;
    setLoading(true);
    try {
      await accept(await api.getSongEditor(song.id));
    } catch (error) {
      await notify(t("editor.failedToOpenEditor", { 0: getErrorMessage(error) }));
    } finally {
      setLoading(false);
    }
  }, [accept, notify, song?.id]);

  useEffect(() => {
    load();
  }, [load]);

  const save = useCallback(async () => {
    if (!song?.id) return;
    setSaving(true);
    try {
      const raw = serializeNotes(document.notes, document.wordBounds);
      await accept(
        await api.saveSongEditor(
          song.id,
          raw,
          document.wordTexts,
          serializeWordBounds(document.wordBounds)
        )
      );
    } catch (error) {
      await notify(t("editor.failedToSaveEditor", { 0: getErrorMessage(error) }));
    } finally {
      setSaving(false);
    }
  }, [accept, document.notes, document.wordBounds, document.wordTexts, notify, song?.id]);

  const restore = useCallback(async () => {
    if (
      !song?.id ||
      !payload?.ai_backup_exists ||
      !(await confirm(t("editor.returnOriginalAiResultManualChangesWillBeLost")))
    )
      return;
    try {
      await accept(await api.resetSongEditor(song.id));
    } catch (error) {
      await notify(t("editor.failedToRestoreAi", { 0: getErrorMessage(error) }));
    }
  }, [accept, confirm, notify, payload?.ai_backup_exists, song?.id]);

  return { dispatch, document, load, loading, payload, restore, save, saving, song };
}
