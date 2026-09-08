import { translateSaved as tr } from "../../i18n/runtime";
import { clamp } from "../../utils/math";
import { formatSafeDate } from "../../utils/time-format";

const ACTIVE = new Set(["processing", "queued", "cancelling"]);
const array = (value) => (Array.isArray(value) ? value : []);
const text = (value) => String(value ?? "").trim();

export const sameId = (a, b) => a != null && b != null && String(a) === String(b);

export const SONG_DROPZONE_ACCEPT = {
  "audio/*": [".mp3", ".wav", ".flac", ".m4a", ".ogg"],
  "application/octet-stream": [".kar", ".mid", ".kfn"]
};

export const formatLibraryDate = (value, locale = "ru-RU") =>
  formatSafeDate(value, (date) => date.toLocaleDateString(locale));

export const formatEta = (seconds) => {
  const value = Math.round(Number(seconds));
  if (!Number.isFinite(value) || value <= 0) return tr("library.counting");
  const minutes = Math.floor(value / 60);
  return minutes
    ? tr("library.minSec", { 0: minutes, 1: value % 60 })
    : tr("library.sec", { 0: value });
};

export const getProcessingProgress = (status, song) =>
  clamp(Number(status?.progress_percent ?? song?.progress_percent) || 0, 0, 100);

export const isProcessingActive = (status) => ACTIVE.has(String(status));
export const hasActiveSongProcessing = (songs) =>
  array(songs).some(({ status } = {}) => isProcessingActive(status));

export const getProcessingSongs = (songs) => {
  const rank = { processing: 0, cancelling: 1, queued: 2 };
  return array(songs)
    .filter((song) => isProcessingActive(song?.status))
    .sort((a, b) => (rank[a.status] ?? 3) - (rank[b.status] ?? 3));
};

export const mergeSongProcessingStatus = (songs, status) =>
  !Array.isArray(songs) || !status?.song_id
    ? (songs ?? [])
    : songs.map((song) =>
        sameId(song?.id, status.song_id)
          ? {
              ...song,
              status: status.status ?? song.status,
              progress_step: status.progress_step ?? song.progress_step,
              progress_percent: status.progress_percent ?? song.progress_percent,
              error_message: status.error_message ?? song.error_message
            }
          : song
      );

export const getLocalVisibleSongs = (songs, hidden) =>
  array(songs).filter((song) => song && typeof song === "object" && !hidden?.has?.(song.id));

export const resolveVisibleSongs = ({ localSongs, room, roomSongs, roomSongsByParticipant }) => {
  if (!room) return array(localSongs);
  const songs = new Map();
  const add = (song, local) => {
    if (!song?.id || songs.has(String(song.id))) return;
    songs.set(
      String(song.id),
      local
        ? { ...song, __roomLocal: true, __roomOwnerId: room.selfId || song.__roomOwnerId }
        : song
    );
  };

  array(localSongs).forEach((song) => add(song, true));
  array(roomSongs).forEach((song) => add(song, false));
  Object.values(roomSongsByParticipant || {})
    .flatMap(array)
    .forEach((song) => add(song, false));
  return [...songs.values()];
};

export const filterSongs = (songs, query) => {
  const needle = text(query).toLowerCase();
  return needle
    ? array(songs).filter((song) =>
        [song?.title, song?.artist, song?.genre]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(needle)
      )
    : array(songs);
};

export const defaultLibraryFilters = Object.freeze({
  sort: "relevance",
  genre: "",
  key: "",
  status: ""
});

const songKey = (song) => text(song?.key_override ?? song?.key);
const normalized = (value) => text(value).toLocaleLowerCase();
const unique = (values) => [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b));
const timestamp = (value) => Date.parse(value) || 0;

export const getLibraryFilterOptions = (songs) => ({
  genres: unique(array(songs).map((song) => text(song?.genre))),
  keys: unique(array(songs).map(songKey))
});

export const arrangeSongs = (songs, query, filters = defaultLibraryFilters) => {
  const result = filterSongs(songs, query).filter(
    (song) =>
      (!filters.genre || normalized(song?.genre) === normalized(filters.genre)) &&
      (!filters.key || normalized(songKey(song)) === normalized(filters.key)) &&
      (!filters.status ||
        (filters.status === "active"
          ? isProcessingActive(song?.status)
          : song?.status === filters.status))
  );
  const compare = {
    title: (a, b) => text(a.title).localeCompare(text(b.title)),
    artist: (a, b) => text(a.artist).localeCompare(text(b.artist)),
    recent: (a, b) => timestamp(b.created_at) - timestamp(a.created_at)
  }[filters.sort];
  return compare ? [...result].sort(compare) : result;
};

export const countReadySongs = (songs) =>
  array(songs).filter(({ status }) => status === "done").length;

export const getSongCardState = (song) => {
  const status = typeof song?.status === "string" && song.status ? song.status : "pending";
  return { status, isWorking: isProcessingActive(status), isReady: status === "done" };
};

export const formatSongKey = (value) => {
  const key = typeof value === "string" ? text(value) : "";
  return key
    ? key.replace(/\s+minor$/i, "m").replace(/\s+major$/i, "maj")
    : tr("library.theTonalityIsDetermined");
};
