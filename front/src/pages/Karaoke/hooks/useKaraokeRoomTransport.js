import { useCallback, useEffect, useRef } from "react";
import useLatestRef from "../../../hooks/useLatestRef";
import { createRoomSyncChannel } from "../../../services/roomSyncChannel";
import { sameId } from "../../../utils/id";
import { formatError } from "../utils/recordingSession";
import {
  classifyDrift,
  createPlayerSyncCommand,
  driftCorrectedRate,
  normalizePlaybackRate
} from "../utils/transport";

const number = (value) => (value == null || value === "" ? NaN : Number(value));

export default function useKaraokeRoomTransport({
  onlineRoom,
  songId,
  instrumentalRef,
  isPlaying,
  navigate,
  seekTo,
  speed,
  stop,
  togglePlay,
  setRecordingError
}) {
  const { room, syncCommand, roomClockNow, roomCommand: command } = onlineRoom || {};
  const channel = useRef(createRoomSyncChannel());
  const seekRef = useLatestRef(seekTo);
  const stopRef = useLatestRef(stop);
  const toggleRef = useLatestRef(togglePlay);

  const broadcast = useCallback(
    (action, position, executeAt = null) => {
      if (!room || !songId || typeof syncCommand !== "function") return;
      Promise.resolve()
        .then(() => syncCommand(createPlayerSyncCommand(action, songId, position, executeAt)))
        .catch(() => {});
    },
    [room, songId, syncCommand]
  );

  useEffect(() => {
    if (!room?.host || !isPlaying || !songId) return;
    const timer = setInterval(() => {
      const position = instrumentalRef.current?.currentTime;
      if (Number.isFinite(position)) broadcast("sync", position);
    }, 500);
    return () => clearInterval(timer);
  }, [broadcast, instrumentalRef, isPlaying, room?.host, songId]);

  useEffect(() => {
    channel.current = createRoomSyncChannel();
  }, [room?.id, songId]);

  useEffect(() => {
    const audio = instrumentalRef.current;
    if (
      room?.host ||
      command?.type !== "karaoke-player" ||
      !command.commandId ||
      !sameId(command.songId, songId) ||
      !audio ||
      !channel.current.acceptCommand(command.commandId)
    ) {
      return;
    }

    const position = number(command.position);
    const sampledAt = number(command.positionAt);
    const sentAt = Number.isFinite(sampledAt) ? sampledAt : number(command.__serverSentAt);
    const receivedAt = number(command.__receivedServerAt);
    const executeAt = number(command.executeAt);
    const serverNow = typeof roomClockNow === "function" ? number(roomClockNow()) : receivedAt;
    const delivery =
      ["play", "sync"].includes(command.action) && Number.isFinite(sentAt) && Number.isFinite(receivedAt)
        ? Math.max(0, (receivedAt - sentAt) / 1000)
        : 0;
    const late =
      command.action === "play" && Number.isFinite(executeAt) && Number.isFinite(serverNow)
        ? Math.max(0, (serverNow - executeAt) / 1000)
        : delivery;
    // `late` is real wall-clock seconds; the song timeline only advances by
    // that much at speed=1. At speed=1.5 the song actually moved late*1.5
    // song-seconds during that same real time -- adding `late` unscaled
    // undercounted it (and overcounted it below 1x), leaving guests
    // systematically behind/ahead of the host whenever speed != 1.
    const target = position + late * speed;

    if (Number.isFinite(target)) {
      if (command.action === "sync") {
        // Ordinary WebSocket jitter on one "sync" tick (arriving every
        // 500ms) routinely swings the estimated delivery delay by tens of
        // milliseconds. A flat hard-seek threshold turned that jitter into
        // an audible seek glitch roughly twice a second on every guest.
        // Reuse the same soft/strong/hard drift classification already used
        // to keep vocals/video aligned to this same instrumental -- small
        // drift nudges playbackRate instead of seeking, so ordinary jitter
        // is absorbed silently and only genuine, sustained drift seeks.
        const drift = audio.currentTime - target;
        const classification = classifyDrift(drift);
        const baseRate = normalizePlaybackRate(speed);
        if (classification === "hard") {
          seekRef.current?.(target, { broadcast: false });
          audio.playbackRate = baseRate;
        } else if (classification === "none") {
          audio.playbackRate = baseRate;
        } else {
          audio.playbackRate = driftCorrectedRate(baseRate, drift, classification);
        }
      } else {
        seekRef.current?.(target, { broadcast: false });
      }
    }

    const actions = {
      play: () => toggleRef.current?.({ broadcast: false, forcePlaying: true }),
      pause: () => toggleRef.current?.({ broadcast: false, forcePlaying: false }),
      stop: async () => {
        const stopped = await stopRef.current?.({ broadcast: false });
        if (stopped) navigate("/", { replace: true, state: { fromKaraokeFade: true } });
        return stopped;
      }
    };
    if (!Object.hasOwn(actions, command.action)) return;

    const run = () =>
      Promise.resolve()
        .then(actions[command.action])
        .catch((error) => setRecordingError(formatError("karaoke.failedToExecuteRoomCommand", error)));
    const delay =
      command.action === "play" && Number.isFinite(executeAt) && Number.isFinite(serverNow)
        ? Math.max(0, executeAt - serverNow)
        : 0;

    if (!delay) return void run();

    let executed = false;
    const timer = setTimeout(() => {
      executed = true;
      run();
    }, delay);
    return () => {
      clearTimeout(timer);
      if (!executed) channel.current.cancelCommand(command.commandId);
    };
  }, [
    command,
    instrumentalRef,
    navigate,
    room?.host,
    roomClockNow,
    seekRef,
    setRecordingError,
    songId,
    speed,
    stopRef,
    toggleRef
  ]);

  return broadcast;
}
