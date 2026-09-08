import { api } from "../api/client";
import { translateSaved } from "../i18n/runtime";
import { isValidSongRevision } from "../services/onlineRoom";
import { getErrorMessage } from "../utils/errors";
import { shouldBroadcastRoomTransferProgress } from "./onlineRoomEffects";

export function createVoiceMeshHandlers({
  voice,
  isCurrentConnection,
  attachRemoteStream,
  setVoiceError,
  removeRemoteAudio,
  participantsRef,
  roomRef,
  hostSongCommandRef,
  pendingSongCommandRef,
  setTransferStatus,
  roomTransferBroadcastRef,
  clientRef,
  librarySyncRef,
  client
}) {
  const handlers = {};
  handlers.onRemoteStream = (participantId, stream) => {
    if (!isCurrentConnection()) {
      stream.getTracks().forEach((track) => track.stop());
      return;
    }
    attachRemoteStream(participantId, stream, () => {
      if (!isCurrentConnection()) return;
      setVoiceError(translateSaved("room.tapAnywhereInTheAppToEnableRoomAudio"));
    });
  };
  handlers.onPeerClosed = (participantId) => {
    if (isCurrentConnection()) removeRemoteAudio(participantId);
  };
  handlers.onPeerError = (participantId, message) => {
    if (!isCurrentConnection()) return;
    const participant = participantsRef.current.find(
      ({ id: participantKey }) => participantKey === participantId
    );
    setVoiceError(`${participant?.name || participantId}: ${message}`);
  };
  handlers.onPeerRecovering = (participantId) => {
    if (!isCurrentConnection()) return;
    const participant = participantsRef.current.find(
      ({ id: participantKey }) => participantKey === participantId
    );
    setVoiceError(
      translateSaved("room.peerConnectionDegradedTryingToRestore", {
        0: participant?.name || participantId
      })
    );
  };
  handlers.onPeerRecovered = () => {
    if (isCurrentConnection()) setVoiceError("");
  };
  handlers.onTransferProgress = ({ participantId, stage, percent, metadata }) => {
    if (!isCurrentConnection()) return;
    const commandId = metadata?.commandId;
    // A library sync has no host/pending-command correlation to check --
    // it's a direct request between two participants -- so its sends are
    // always shown (this is also how the sender sees a recipient's
    // download percentage while serving voice.onSongPullRequest above).
    if (metadata?.kind !== "library-song-package") {
      const currentCommandId = roomRef.current?.host
        ? hostSongCommandRef.current?.commandId
        : pendingSongCommandRef.current?.commandId;
      if (commandId && commandId !== currentCommandId) return;
    }
    const normalizedPercent = Number(percent) || 0;
    setTransferStatus({
      participantId,
      songId: metadata?.songId,
      commandId,
      stage,
      percent: normalizedPercent
    });
    if (roomRef.current?.host && metadata?.kind === "song-package") {
      const roomProgress = {
        commandId,
        stage,
        percent: normalizedPercent,
        at: Date.now()
      };
      if (
        shouldBroadcastRoomTransferProgress(
          roomTransferBroadcastRef.current,
          roomProgress,
          roomProgress.at
        )
      ) {
        roomTransferBroadcastRef.current = roomProgress;
        clientRef.current?.send("sync", {
          state: {
            type: "song-transfer-status",
            participantId: "room",
            songId: metadata.songId,
            commandId,
            stage: "waiting",
            percent: normalizedPercent
          }
        });
      }
    }
  };
  const canAcceptSongPackage = (participantId, metadata) => {
    const pending = pendingSongCommandRef.current;
    return (
      isCurrentConnection() &&
      metadata?.kind === "song-package" &&
      !!metadata.songId &&
      !!metadata.commandId &&
      pending?.commandId === metadata.commandId &&
      pending.songId === metadata.songId &&
      pending.revision === metadata.revision &&
      !pending.__originatedHere &&
      participantsRef.current.some(
        (participant) => participant.id === participantId && participant.role === "host"
      )
    );
  };
  const canAcceptLibrarySongPackage = (participantId, metadata) => {
    const pending = librarySyncRef.current;
    return (
      isCurrentConnection() &&
      !!pending &&
      metadata?.kind === "library-song-package" &&
      pending.commandId === metadata.commandId &&
      pending.songId === metadata.songId &&
      pending.ownerId === participantId
    );
  };
  handlers.canAcceptFile = (participantId, metadata) =>
    metadata?.kind === "library-song-package"
      ? canAcceptLibrarySongPackage(participantId, metadata)
      : canAcceptSongPackage(participantId, metadata);

  // Shared by both onFile handlers below: marks the transfer "importing",
  // hands the blob to the API, then confirms the imported copy actually
  // landed on the revision the sender promised before either handler treats
  // it as usable. `resolveVerifyId` lets a caller check a different song id
  // than the one requested (the library import can resolve to an existing
  // local copy under its own id) -- it defaults to the requested id.
  const importAndVerifySongPackage = async ({
    participantId,
    blob,
    metadata,
    signal,
    resolveVerifyId
  }) => {
    setTransferStatus({
      participantId,
      songId: metadata.songId,
      commandId: metadata.commandId,
      stage: "importing",
      percent: 100
    });
    const importedSong = await api.importSongPackage(blob, metadata.filename, {
      ...(signal ? { signal } : {}),
      expectedRevision: metadata.revision
    });
    const verifyId = resolveVerifyId ? resolveVerifyId(importedSong) : metadata.songId;
    const imported = await api.getSongRevision(verifyId);
    if (imported?.revision !== metadata.revision)
      throw new Error(translateSaved("room.importedSongVersionDoesNotMatchTheRoomVersion"));
    return importedSong;
  };

  const handleLibrarySongFile = async (participantId, blob, metadata, signal) => {
    const pending = librarySyncRef.current;
    if (!canAcceptLibrarySongPackage(participantId, metadata))
      throw new Error(translateSaved("room.receivingThisSongIsNoLongerAllowed"));
    try {
      const importedSong = await importAndVerifySongPackage({
        participantId,
        blob,
        metadata,
        signal,
        resolveVerifyId: (song) => song?.id || metadata.songId
      });
      const localSongId = importedSong?.id || metadata.songId;
      if (!isCurrentConnection() || librarySyncRef.current !== pending) return false;
      setTransferStatus({
        participantId,
        songId: metadata.songId,
        commandId: metadata.commandId,
        stage: "complete",
        percent: 100
      });
      pending.resolve?.(localSongId);
      return true;
    } catch (error) {
      if (signal?.aborted) return false;
      if (isCurrentConnection() && librarySyncRef.current === pending) {
        pending.reject?.(
          new Error(translateSaved("room.couldNotReceiveSong", { 0: getErrorMessage(error) }))
        );
      }
      throw error;
    }
  };

  const handleKaraokeSongFile = async (participantId, blob, metadata, signal) => {
    const pendingCommand = pendingSongCommandRef.current;
    if (!canAcceptSongPackage(participantId, metadata))
      throw new Error(translateSaved("room.receivingThisSongPackageIsNoLongerAllowed"));
    try {
      await importAndVerifySongPackage({ participantId, blob, metadata, signal });
      if (!isCurrentConnection() || pendingSongCommandRef.current?.commandId !== metadata.commandId)
        return false;
      setTransferStatus({
        participantId,
        songId: metadata.songId,
        commandId: metadata.commandId,
        stage: "complete",
        percent: 100
      });
      if (
        pendingCommand?.commandId === metadata.commandId &&
        pendingCommand.songId === metadata.songId
      ) {
        client.send("sync", {
          state: {
            type: "song-ready",
            commandId: pendingCommand.commandId,
            songId: pendingCommand.songId,
            revision: pendingCommand.revision,
            requesterId: roomRef.current?.selfId
          }
        });
      }
      return true;
    } catch (error) {
      if (signal?.aborted) return false;
      if (isCurrentConnection()) {
        setTransferStatus({
          participantId,
          songId: metadata.songId,
          commandId: metadata.commandId,
          stage: "error",
          error: translateSaved("room.failedToImportSong", {
            0: getErrorMessage(error)
          }),
          percent: 0
        });
      }
      throw error;
    }
  };
  handlers.onFile = (participantId, blob, metadata, signal) =>
    metadata?.kind === "library-song-package"
      ? handleLibrarySongFile(participantId, blob, metadata, signal)
      : handleKaraokeSongFile(participantId, blob, metadata, signal);

  // Any participant can ask any other participant for a song straight over
  // the already-open peer-to-peer channel -- this is the library "sync on
  // demand" path (OnlineRoomDock/song-card), independent of the host-driven
  // "start karaoke" push above, so it works for guest-to-guest transfers too.
  handlers.onSongPullRequest = async (participantId, channel, message) => {
    if (!isCurrentConnection()) return;
    let blob;
    try {
      const revisionPayload = await api.getSongRevision(message.songId);
      const revision = revisionPayload?.revision;
      if (!isValidSongRevision(revision))
        throw new Error(translateSaved("room.songIsUnavailableForTransfer"));
      if (!isCurrentConnection()) return;
      blob = await api.exportSongPackage(message.songId, revision);
      if (!isCurrentConnection()) return;
      await voice.sendFile(participantId, blob, {
        resumable: true,
        kind: "library-song-package",
        songId: message.songId,
        commandId: message.commandId,
        revision,
        filename: `${message.songId}.karaoke.zip`
      });
    } catch (error) {
      voice.sendSongSyncError(participantId, message.commandId, getErrorMessage(error));
    } finally {
      await blob?.cleanup?.();
    }
  };
  handlers.onSongPullError = (participantId, message) => {
    const pending = librarySyncRef.current;
    if (!pending || pending.commandId !== message.commandId || pending.ownerId !== participantId)
      return;
    pending.reject?.(
      new Error(message.error || translateSaved("room.participantCouldNotSendTheSong"))
    );
  };

  return handlers;
}
