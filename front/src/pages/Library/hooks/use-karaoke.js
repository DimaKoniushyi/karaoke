import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../../api/client";
import { translateSaved as tr } from "../../../i18n/runtime";
import { getErrorMessage } from "../../../utils/errors";
import { setGlobalRouteBlackout } from "../../../utils/route-blackout";
import { isProcessingActive, sameId } from "../utils";

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function getLocalSongId(song, room, localSongs, refresh) {
  if (!room?.room || localSongs.some(({ id }) => sameId(id, song.id))) return song.id;

  const resolved =
    song.__roomRevision && (await api.resolveSongRevision(song.__roomRevision).catch(() => null));
  if (resolved?.song_id) return resolved.song_id;

  const id = await room.requestSongSync(song.id, song.__roomOwnerId, { roomWide: true });
  if (!id) throw new Error(tr("library.couldNotReceiveSongFromParticipant"));
  await refresh();
  return typeof id === "string" ? id : song.id;
}

function useTransition(returning) {
  const [transitioning, setTransitioning] = useState(returning);
  const lock = useRef(returning);
  const set = useCallback((active) => {
    lock.current = active;
    setTransitioning(active);
    setGlobalRouteBlackout(active);
  }, []);

  useEffect(() => {
    if (!returning) {
      if (lock.current) set(false);
      return;
    }
    const timer = setTimeout(() => set(false), 120);
    return () => clearTimeout(timer);
  }, [returning, set]);

  return [transitioning, lock, set];
}

function useRemoteSync(room, localSongs, songs, refresh) {
  const statuses = useRef(new Map());
  const queue = useRef(Promise.resolve());
  const generation = useRef(0);

  useEffect(() => {
    const version = ++generation.current;
    if (!room?.room) return statuses.current.clear();

    const local = new Set(localSongs.map(({ id }) => String(id)));
    const next = new Map();

    for (const song of songs) {
      if (!song?.id || song.__roomOwnerId === room.room.selfId) continue;
      const key = String(song.id);
      const previous = statuses.current.get(key);
      next.set(key, song.status);

      if (
        !isProcessingActive(previous) ||
        song.status !== "done" ||
        !song.__roomRevision ||
        local.has(key)
      ) {
        continue;
      }

      queue.current = queue.current
        .catch(() => {})
        .then(async () => {
          if (version !== generation.current) return;
          if (!(await room.requestSongSync(song.id, song.__roomOwnerId))) throw new Error();
          if (version === generation.current) await refresh();
        })
        .catch(() => {
          if (version === generation.current) statuses.current.set(key, previous);
        });
    }

    statuses.current = next;
  }, [room, localSongs, songs, refresh]);
}

function useRoomRequest(room, songs, openKaraoke) {
  const handled = useRef();

  useEffect(() => {
    const command = room?.roomCommand;
    if (
      !room?.room?.host ||
      command?.type !== "karaoke-request" ||
      !command.__eventId ||
      handled.current === command.__eventId
    ) {
      return;
    }

    let active = true;
    const song = songs.find(
      (song) =>
        sameId(song?.id, command.songId) ||
        (command.revision && song?.__roomRevision === command.revision)
    ) || {
      id: command.songId,
      __roomOwnerId: command.ownerId,
      __roomRevision: command.revision
    };

    (async () => {
      while (active && !(await openKaraoke(song))) await wait(50);
      if (active) handled.current = command.__eventId;
    })();

    return () => {
      active = false;
    };
  }, [room?.room?.host, room?.roomCommand, songs, openKaraoke]);
}

export default function useLibraryKaraoke({
  room,
  localSongs,
  visibleSongs,
  refresh,
  navigate,
  alert,
  returning
}) {
  const [transitioning, lock, transition] = useTransition(returning);
  useRemoteSync(room, localSongs, visibleSongs, refresh);

  const openKaraoke = useCallback(
    async (song) => {
      if (lock.current) return false;
      lock.current = true;

      try {
        if (room?.room && !room.room.host) {
          await room.openKaraoke(song.id, {
            ownerId: song.__roomOwnerId || room.room.selfId,
            revision: song.__roomRevision
          });
          lock.current = false;
          return true;
        }

        const id = await getLocalSongId(song, room, localSongs, refresh);
        // Reaching here means room?.room is either falsy or host -- see the
        // guest branch above, which already returned for the non-host case.
        if (room?.room && !(await room.openKaraoke(id))) {
          lock.current = false;
          return true;
        }

        transition(true);
        await wait(920);
        navigate("/karaoke", { state: { songId: id, autoPlay: true } });
      } catch (error) {
        transition(false);
        await alert(tr("library.failedToOpenSong", { 0: getErrorMessage(error) }));
      }
      return true;
    },
    [room, localSongs, refresh, navigate, alert, lock, transition]
  );

  useRoomRequest(room, visibleSongs, openKaraoke);
  return { transitioning, openKaraoke };
}
