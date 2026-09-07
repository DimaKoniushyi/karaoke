import { Music2, Piano, Save } from "lucide-react";
import { useEffect, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../../../../api/client";
import { useAppDialog } from "../../../../contexts/AppDialog";
import { translateSaved as tr } from "../../../../i18n/runtime";
import {
  Button,
  Modal,
  RenderFormikFields,
  Stack,
  Typography,
  useGetForm
} from "../../../../theme/ui";
import { getErrorMessage } from "../../../../utils/errors";
import getRows from "./rows";
import { createSongPayload, validateSongSettings } from "./utils";

export default function SongSettings({ song, onClose, onSaved }) {
  const { alert } = useAppDialog();
  const navigate = useNavigate();
  const loaded = useRef();
  const form = useGetForm({
    initialValues: {},
    enableReinitialize: false,
    onSubmit: async (values) => {
      const error = validateSongSettings(values);
      if (error) return alert(error);
      try {
        const updated = await api.updateSong(song.id, createSongPayload(values, song));
        if (updated) form.setValues((values) => ({ ...values, ...updated }));
        await onSaved?.();
      } catch (error) {
        await alert(tr("library.failedToSave", { 0: getErrorMessage(error) }));
      }
    }
  });

  useEffect(() => {
    if (loaded.current === song?.id) return;
    loaded.current = song?.id;
    form.resetForm({ values: song ? { ...song } : {} });
  }, [song, form.resetForm]);

  return (
    <Modal
      isOpen
      portal
      onClose={onClose}
      ariaLabel={tr("library.songSettings2", { 0: song?.title || "" }).trim()}
      titleProps={{
        icon: Music2,
        eyebrow: tr("library.karaokeEditor"),
        title: tr("library.songSettings"),
        description: song?.title || tr("library.loadingSongData"),
        actions: song && (
          <Button
            variant="contained"
            startIcon={<Save />}
            disabled={form.isSubmitting}
            onClick={form.submitForm}
          >
            {tr(form.isSubmitting ? "library.saving" : "library.save")}
          </Button>
        )
      }}
    >
      <Stack sx={{ padding: "var(--space-5)" }}>
        {!song ? (
          <Typography role="alert" tone="danger">
            {tr("library.songNotFoundItMayHaveBeenDeleted")}
          </Typography>
        ) : form.values.id !== song.id ? (
          <Typography tone="muted">{tr("library.preparingTheSettings")}</Typography>
        ) : (
          <Stack gap={1}>
            <form onSubmit={form.handleSubmit} noValidate>
              <RenderFormikFields formik={form} items={getRows()} />
            </form>
            {song.status === "done" && (
              <Stack gap={0.5}>
                <Typography sx={{ fontWeight: 800 }}>{tr("library.melodyAndLyrics")}</Typography>
                <Typography variant="body2" tone="muted">
                  {tr("library.openThePianoRollEditorToAdjustNotesDurations")}
                </Typography>
                <Button
                  variant="outlined"
                  startIcon={<Piano />}
                  onClick={() => {
                    onClose?.();
                    navigate(`/editor/${song.id}`);
                  }}
                >
                  {tr("library.openEditor")}
                </Button>
              </Stack>
            )}
          </Stack>
        )}
      </Stack>
    </Modal>
  );
}
