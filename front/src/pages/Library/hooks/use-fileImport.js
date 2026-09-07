import { useCallback, useState } from "react";
import { api } from "../../../api/client";
import { isAmbiguousTransportError } from "../../../api/core";
import useExclusiveAsyncAction from "../../../hooks/useExclusiveAsyncAction";
import { translateSaved as tr } from "../../../i18n/runtime";
import { getErrorMessage } from "../../../utils/errors";

const DEFAULT_MODE = "auto";
const MODES = new Set([DEFAULT_MODE, "fast", "quality"]);
const mode = (value) => (MODES.has(value) ? value : DEFAULT_MODE);

export const suggestedIdentity = (file, metadata = {}) => {
  const stem = file.name.replace(/\.[^.]+$/, "").replace(/\s*\(\d+\)\s*$/, "").trim();
  const [artist = "", title = stem] = stem.split(/\s+[-–—]\s+/, 2).map((part) => part.trim());
  const split = /\s+[-–—]\s+/.test(stem);
  return {
    file,
    coverUrl: metadata.cover_data_url || "",
    artist: metadata.artist || (split ? artist : ""),
    title: metadata.title || (split ? title : stem),
    processingMode: DEFAULT_MODE
  };
};

export default function useLibraryFileImport({ notify, onStarted }) {
  const { pending, run } = useExclusiveAsyncAction();
  const [review, setReview] = useState(null);

  const process = useCallback(
    (items) =>
      run(async () => {
        for (const item of items) {
          let song;
          try {
            song = await api.addSong(item.file, item.title, item.artist);
            await api.processSong(song.id, mode(item.processingMode));
            onStarted(song);
          } catch (error) {
            if (song && isAmbiguousTransportError(error)) {
              onStarted(song);
              await notify(
                tr("library.songAddedButBackendDidNotConfirmProcessingStarted", {
                  0: getErrorMessage(error)
                })
              );
              continue;
            }
            if (song) await api.deleteSong(song.id).catch(() => {});
            await notify(
              tr(song ? "library.couldNotStartSongProcessing" : "library.couldNotAddSong", {
                0: `${item.file.name}: ${getErrorMessage(error)}`
              })
            );
          }
        }
      }),
    [notify, onStarted, run]
  );

  const advance = useCallback(
    (approved) =>
      setReview((review) => {
        if (!review) return null;
        const index = review.index + 1;
        if (index < review.items.length) return { ...review, index, approved };
        queueMicrotask(() => process(approved));
        return null;
      }),
    [process]
  );

  const importFile = useCallback(
    (source) => {
      const input = Array.isArray(source) ? null : source.currentTarget;
      const files = Array.isArray(source) ? source : [...(input?.files || [])];
      if (input) input.value = "";
      if (!files.length) return;

      return run(async () => {
        const metadata = await Promise.all(
          files.map((file) => api.inspectSongIdentity(file).catch(() => ({})))
        );
        const items = files.map((file, index) => suggestedIdentity(file, metadata[index]));
        setReview({ items, index: 0, approved: [] });
      });
    },
    [run]
  );

  const confirmDraft = useCallback(
    (values) => {
      const draft = review?.items?.[review.index];
      const item = draft && { ...draft, ...values };
      const title = String(item?.title || "").trim();
      if (!title) return;
      advance([
        ...review.approved,
        { ...item, title, artist: String(item.artist || "").trim() }
      ]);
    },
    [advance, review]
  );

  return {
    review,
    importing: pending || !!review,
    importFile,
    confirmDraft,
    cancelDraft: () => review && advance(review.approved)
  };
}
