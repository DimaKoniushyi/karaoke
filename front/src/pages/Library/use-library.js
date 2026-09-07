import { useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { api } from "../../api/client";
import { useAppDialog } from "../../contexts/AppDialog";
import { useOnlineRoom } from "../../contexts/OnlineRoomContext";
import useAppSettings from "../../hooks/useAppSettings";
import { usePolling } from "../../hooks/usePolling";
import { getOnlineNameMessage } from "../../hooks/useRequireOnlineName";
import { translateSaved as tr } from "../../i18n/runtime";
import { queryKeys } from "../../query-client";
import { getErrorMessage } from "../../utils/errors";
import useLibraryFileImport from "./hooks/use-fileImport";
import useLibraryKaraoke from "./hooks/use-karaoke";
import useLibraryProcessing from "./hooks/use-processing";
import useLibraryRoomSync from "./hooks/use-room-sync";
import {
  arrangeSongs,
  countReadySongs,
  defaultLibraryFilters,
  getLibraryFilterOptions,
  getLocalVisibleSongs,
  resolveVisibleSongs
} from "./utils";

export default function useLibrary() {
  const location = useLocation();
  const navigate = useNavigate();
  const dialog = useAppDialog();
  const online = useOnlineRoom();
  const app = useAppSettings();
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState(defaultLibraryFilters);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [hiddenSongIds, setHiddenSongIds] = useState(new Set());
  const songsQuery = usePolling(api.listSongs, 0, [], { queryKey: queryKeys.songs });
  const processing = useLibraryProcessing(songsQuery, dialog);
  const { room } = online;
  const roomUi = online.roomUi || {};

  const localSongs = useMemo(
    () => getLocalVisibleSongs(processing.currentSongs, hiddenSongIds),
    [processing.currentSongs, hiddenSongIds]
  );

  const visibleSongs = useMemo(
    () =>
      resolveVisibleSongs({
        localSongs,
        room,
        roomSongs: roomUi.songs,
        roomSongsByParticipant: roomUi.songsByParticipant
      }),
    [localSongs, room, roomUi.songs, roomUi.songsByParticipant]
  );

  // Split from the filtered/sorted list below: filterOptions and readyCount
  // only depend on visibleSongs, not on query/filters, so they shouldn't be
  // recomputed on every keystroke in the search box.
  const visibleSongsSummary = useMemo(
    () => ({
      filterOptions: getLibraryFilterOptions(visibleSongs),
      readyCount: countReadySongs(visibleSongs)
    }),
    [visibleSongs]
  );
  const view = useMemo(
    () => ({
      songs: arrangeSongs(visibleSongs, query, filters),
      ...visibleSongsSummary
    }),
    [visibleSongs, query, filters, visibleSongsSummary]
  );

  const karaoke = useLibraryKaraoke({
    room: online,
    localSongs,
    visibleSongs,
    refresh: songsQuery.refresh,
    navigate,
    alert: dialog.alert,
    returning: !!location.state?.fromKaraokeFade
  });

  const fileImport = useLibraryFileImport({
    notify: dialog.alert,
    onStarted: (song) => {
      processing.track(song);
      songsQuery.refresh();
    }
  });

  useLibraryRoomSync({
    localSongs,
    query,
    filters,
    filtersOpen,
    room,
    roomEventId: roomUi.__eventId,
    roomQuery: roomUi.query,
    roomFilters: roomUi.filters,
    roomFiltersOpen: roomUi.libraryFiltersOpen,
    participantCount: online.participants?.length,
    setQuery,
    setFilters,
    setFiltersOpen,
    syncUi: online.syncUi
  });

  const canOpenRoom = async () => {
    try {
      const name = (await app.reloadSettings())?.online_name?.trim();
      if (!name) {
        await dialog.alert(getOnlineNameMessage());
        return false;
      }
      return true;
    } catch (error) {
      await dialog.alert(
        tr("library.failedToCheckOnlineModeSettings", { 0: getErrorMessage(error) })
      );
      return false;
    }
  };

  return {
    query,
    setQuery,
    filters,
    setFilters,
    filtersOpen,
    setFiltersOpen,
    fileImport,
    processing,
    songs: processing.currentSongs,
    filteredSongs: view.songs,
    filterOptions: view.filterOptions,
    readyCount: view.readyCount,
    totalCount: visibleSongs.length,
    songsError: songsQuery.error,
    refreshSongs: songsQuery.refresh,
    setHiddenSongIds,
    canManageLibrary: !room || room.host,
    room: online,
    transferStatuses: online.transferStatuses,
    online: {
      canOpen: canOpenRoom,
      name: app.settings?.online_name?.trim() || "",
      roomActive: !!room,
      setName: (online_name) => app.updateSettings?.((settings) => ({ ...settings, online_name }))
    },
    transitioning: karaoke.transitioning,
    openKaraoke: karaoke.openKaraoke
  };
}
