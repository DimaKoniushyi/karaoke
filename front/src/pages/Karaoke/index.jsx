import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { api } from "../../api/client";
import { usePolling } from "../../hooks/usePolling";
import { queryKeys } from "../../query-client";
import { POLLING_INTERVALS } from "../../runtime-config";
import { sameId } from "../../utils/id";
import KaraokeLoadState from "./karaoke-load-state";
import KaraokeSession from "./karaoke-session";

function useRoutedSong(songs, songId) {
  const list = Array.isArray(songs) ? songs : [];
  const listed =
    songId == null
      ? list.find((song) => song?.status === "done")
      : list.find((song) => sameId(song?.id, songId));
  const [loaded, setLoaded] = useState(null);

  useEffect(() => {
    if (songId == null || !Array.isArray(songs) || listed) return void setLoaded(null);
    let active = true;
    api.getSong(songId).then(
      (song) => active && setLoaded(song),
      () => active && setLoaded(null)
    );
    return () => { active = false; };
  }, [listed?.id, songId, songs]);

  return listed || (sameId(loaded?.id, songId) ? loaded : null);
}

function useKaraokeResult(song) {
  const [state, setState] = useState({ result: null, loading: false, error: null });
  const { id, status, updated_at: updatedAt } = song || {};

  useEffect(() => {
    if (!id || status !== "done") {
      setState({ result: null, loading: false, error: null });
      return;
    }
    let active = true;
    setState({ result: null, loading: true, error: null });
    api.getResult(id).then(
      (result) => active && setState({ result, loading: false, error: null }),
      (error) => active && setState({ result: null, loading: false, error })
    );
    return () => { active = false; };
  }, [id, status, updatedAt]);

  return state;
}


export default function Karaoke({ onOpenAppSettings }) {
  const { state = {} } = useLocation();
  const { data: songs, error: songsError } = usePolling(
    api.listSongs,
    POLLING_INTERVALS.settings,
    [],
    { queryKey: queryKeys.songs }
  );
  const songId = state.songId ?? null;
  const song = useRoutedSong(songs, songId);
  const resultState = useKaraokeResult(song);

  if (
    songsError ||
    !songs ||
    !song ||
    song.status !== "done" ||
    resultState.loading ||
    resultState.error ||
    !resultState.result
  ) {
    return (
      <KaraokeLoadState
        songs={songs}
        songsError={songsError}
        song={song}
        songId={songId}
        result={resultState.result}
        resultLoading={resultState.loading}
        resultError={resultState.error}
      />
    );
  }

  return (
    <KaraokeSession
      key={String(song.id)}
      song={song}
      lyricsSync={resultState.result.lyrics_sync}
      autoStartRequested={Boolean(state.autoPlay)}
      roomPrepared={Boolean(state.roomPrepared)}
      onOpenAppSettings={onOpenAppSettings}
    />
  );
}
