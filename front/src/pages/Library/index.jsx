import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { useLocation } from "react-router-dom";
import PerformanceAnalysisModal from "../../components/PerformanceAnalysisModal/index.jsx";
import { useAppDialog } from "../../contexts/AppDialog";
import { Box, Stack } from "../../theme/ui";
import QuantumFieldBackdrop from "./backdrop/index.jsx";
import LibraryHero from "./hero";
import useLibraryRecordings from "./hooks/use-recordings";
import useLibrarySongActions from "./hooks/use-song-actions";
import SongsGrid from "./songs-grid";
import useLibrary from "./use-library";
import { sameId } from "./utils";

const SongSettings = lazy(() => import("./modals/song-settings"));
const AddSongModal = lazy(() => import("./modals/add-song"));
const ProcessingModal = lazy(() => import("./modals/processing/index.jsx"));
const RecordingsModal = lazy(() => import("./modals/recordings"));
const OnlineRoomModal = lazy(() => import("./modals/online-room/index.jsx"));

const emptyAnalysis = { analysisRecordingId: null, analysisRecordings: [] };

export default function Library() {
  const location = useLocation();
  const dialog = useAppDialog();
  const state = useLibrary();
  const recordings = useLibraryRecordings(dialog);
  const [settingsSongId, setSettingsSongId] = useState(null);
  const [onlineOpen, setOnlineOpen] = useState(false);
  const [analysis, setAnalysis] = useState(() => ({
    ...emptyAnalysis,
    analysisRecordingId: location.state?.analysisRecordingId || null
  }));

  useEffect(() => {
    const id = location.state?.analysisRecordingId;
    if (id) setAnalysis((value) => ({ ...value, analysisRecordingId: id }));
  }, [location.state?.analysisRecordingId]);

  const settingsSong = useMemo(
    () => state.songs.find(({ id }) => sameId(id, settingsSongId)),
    [state.songs, settingsSongId]
  );

  const songActions = useLibrarySongActions({
    confirmDialog: dialog.confirm,
    notify: dialog.alert,
    onChanged: state.refreshSongs,
    processingSongId: state.processing.song?.id,
    recordingsSongId: recordings.song?.id,
    setHiddenSongIds: state.setHiddenSongIds,
    setProcessingSong: state.processing.track,
    setRecordingsSong: recordings.setSong
  });

  const openRoom = async () => {
    if (await state.online.canOpen()) setOnlineOpen(true);
  };

  return (
    <Stack align="center" sx={{ position: "relative", minBlockSize: "100vh" }}>
      {!state.processing.active && <QuantumFieldBackdrop />}

      <Stack sx={{ paddingInline: "var(--library-gutter)", position: "relative" }}>
        <LibraryHero
          songCount={state.totalCount}
          readyCount={state.readyCount}
          canManageLibrary={state.canManageLibrary}
          importing={state.fileImport.importing}
          onFileChosen={state.fileImport.importFile}
          onOpenRoom={openRoom}
          roomActive={state.online.roomActive}
          query={state.query}
          setQuery={state.setQuery}
          filters={state.filters}
          filtersOpen={state.filtersOpen}
          filterOptions={state.filterOptions}
          setFilters={state.setFilters}
          setFiltersOpen={state.setFiltersOpen}
        />

        <SongsGrid
          songs={state.filteredSongs}
          error={state.songsError}
          transferStatuses={state.transferStatuses}
          canManageLibrary={state.canManageLibrary}
          fileImport={state.fileImport}
          processing={state.processing}
          recordings={recordings}
          openKaraoke={state.openKaraoke}
          onOpenSettings={setSettingsSongId}
          songActions={songActions}
        />
      </Stack>

      <Box
        aria-hidden
        data-role="library-transition-blackout"
        sx={{
          position: "fixed",
          inset: 0,
          zIndex: "var(--z-overlay)",
          pointerEvents: "none",
          background: "#000",
          opacity: +state.transitioning,
          transition: "opacity 180ms ease"
        }}
      />

      {onlineOpen && (
        <Suspense>
          <OnlineRoomModal
            onlineName={state.online.name}
            onOnlineNameChange={state.online.setName}
            onClose={() => setOnlineOpen(false)}
          />
        </Suspense>
      )}

      <Suspense>
        <RecordingsModal
          song={recordings.song}
          recordings={recordings.items}
          error={recordings.error}
          onClose={() => recordings.setSong(null)}
          onDelete={recordings.delete}
          onAnalyze={(recording) => {
            setAnalysis({
              analysisRecordingId: recording.id,
              analysisRecordings: recordings.items
            });
            recordings.setSong(null);
          }}
        />
      </Suspense>

      <Suspense>
        <AddSongModal
          review={state.fileImport.review}
          onCancel={state.fileImport.cancelDraft}
          onConfirm={state.fileImport.confirmDraft}
        />
      </Suspense>

      {settingsSongId && (
        <Suspense>
          <SongSettings
            song={settingsSong}
            onSaved={state.refreshSongs}
            onClose={() => setSettingsSongId(null)}
          />
        </Suspense>
      )}

      {analysis.analysisRecordingId && (
        <Suspense>
          <PerformanceAnalysisModal
            recordingId={analysis.analysisRecordingId}
            recordings={analysis.analysisRecordings}
            onClose={() => setAnalysis(emptyAnalysis)}
            onDone={() => setAnalysis(emptyAnalysis)}
            onDeleted={() => setAnalysis(emptyAnalysis)}
          />
        </Suspense>
      )}

      {!state.fileImport.review && (
        <Suspense>
          <ProcessingModal
            song={state.processing.song}
            songs={state.processing.songs}
            status={state.processing.status}
            onSelectSong={state.processing.track}
            onClose={state.processing.close}
            onCancel={state.processing.cancel}
            onOpenKaraoke={state.openKaraoke}
          />
        </Suspense>
      )}
    </Stack>
  );
}
