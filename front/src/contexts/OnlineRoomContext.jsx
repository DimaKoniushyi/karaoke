import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState
} from "react";
import { api } from "../api/client";
import { translateSaved } from "../i18n/runtime";
import {
  createHostToken,
  createRoomId,
  OnlineRoomClient,
  OnlineVoiceMesh
} from "../services/onlineRoom";
import { AUDIO_SETTINGS_CHANGED_EVENT } from "../utils/audioSettingsEvents";
import { getErrorMessage } from "../utils/errors";
import useApplicationAudioMute from "./hooks/useApplicationAudioMute";
import useOnlineRoomAudio from "./hooks/useOnlineRoomAudio";
import useOnlineRoomCommands from "./hooks/useOnlineRoomCommands";
import useOnlineRoomHardwareSuspension from "./hooks/useOnlineRoomHardwareSuspension";
import useOnlineRoomParticipantControls from "./hooks/useOnlineRoomParticipantControls";
import useOnlineRoomValue from "./hooks/useOnlineRoomValue";
import useSpeakingLevels from "./hooks/useSpeakingLevels";
import { requestSongSync as requestLibrarySong } from "./onlineRoomActions";
import {
  playConnectionLostSound,
  playParticipantJoinedSound,
  playParticipantLeftSound
} from "./onlineRoomChime";
import { normalizeParticipantEffects, normalizeParticipantEffectPatch } from "./onlineRoomEffects";
import { createOnlineRoomMessageHandler } from "./onlineRoomMessages";
import { createVoiceMeshHandlers } from "./onlineRoomVoiceHandlers";

// Keep one context identity across Vite Fast Refresh. Recreating the context
// while a room is active leaves already-mounted providers on the old object,
// consumers receive null, the error boundary remounts the app and WebSocket
// cleanup makes it look as if karaoke playback kicked the user from the room.
const hotData = import.meta.hot?.data;
const OnlineRoomContext = hotData?.onlineRoomContext || createContext(null);
if (hotData) hotData.onlineRoomContext = OnlineRoomContext;
// Speaking levels update on a ~70ms meter tick while anyone's mic is live,
// which is far more often than the rest of the room state changes. Keeping
// them out of the main context value means a room-wide voice call doesn't
// re-render every consumer of useOnlineRoom() (Karaoke, Library, OnlineRoom
// pages, navigation hooks) dozens of times a second -- only the one place
// that actually renders speaking indicators (OnlineRoomDock) subscribes to
// this context and re-renders on those ticks.
const OnlineRoomSpeakingContext = createContext({ localSpeakingLevel: 0, speakingLevels: {} });
export {
  normalizeParticipantEffects,
  shouldBroadcastRoomTransferProgress
} from "./onlineRoomEffects";

export function OnlineRoomProvider({ children }) {
  const clientRef = useRef(null);
  const unsubscribeRef = useRef(null);
  const voiceRef = useRef(null);
  const roomUiRef = useRef({});
  const microphoneMutedRef = useRef(false);
  const roomRef = useRef(null);
  const mutedPeopleRef = useRef(new Set());
  const roomSoundMutedRef = useRef(false);
  const participantVolumesRef = useRef({});
  const intentionalDisconnectRef = useRef(false);
  const pendingSongCommandRef = useRef(null);
  const hostSongCommandRef = useRef(null);
  const songExportsRef = useRef(new Map());
  // File transfer progress can update once per 32 KiB chunk. Relaying every
  // chunk through the signaling WebSocket exceeds the Worker's room rate
  // limit on ordinary song archives and disconnects the host (which closes
  // the whole room). Keep the detailed progress local and publish a bounded
  // sample to the room.
  const roomTransferBroadcastRef = useRef(null);
  // Ad-hoc "sync this song from a participant's library" request, separate
  // from pendingSongCommandRef (which only tracks the host-driven "start
  // karaoke" push). One sync in flight at a time, same as song processing.
  const librarySyncRef = useRef(null);
  const connectionTokenRef = useRef(null);
  const [room, setRoomState] = useState(null);
  const participantsRef = useRef([]);
  const [participants, setParticipantsState] = useState([]);
  const setParticipants = useCallback((next) => {
    setParticipantsState((current) => {
      const value = typeof next === "function" ? next(current) : next;
      participantsRef.current = value;
      return value;
    });
  }, []);
  const [mutedPeople, setMutedPeople] = useState(() => new Set());
  const [effectPeople, setEffectPeople] = useState(() => new Set());
  const [participantVolumes, setParticipantVolumes] = useState({});
  const [microphoneMuted, setMicrophoneMutedState] = useState(false);
  const [roomSoundMuted, setRoomSoundMutedState] = useState(false);
  const [roomUi, setRoomUi] = useState({});
  const [roomCommand, setRoomCommand] = useState(null);
  const [voiceError, setVoiceError] = useState("");
  const [transferStatuses, setTransferStatuses] = useState(() => new Map());
  const setTransferStatus = useCallback((status) => {
    setTransferStatuses((current) => {
      if (!status) return new Map();
      const next = new Map(current);
      const key = status.participantId || "room";
      if (["complete", "clear"].includes(status.stage)) next.delete(key);
      else next.set(key, status);
      return next;
    });
  }, []);
  const activeTransfer = useMemo(() => {
    const values = [...transferStatuses.values()];
    return values.find((item) => item.stage === "error") || values.at(-1) || null;
  }, [transferStatuses]);
  const transferStatus = useMemo(() => {
    if (!activeTransfer) return null;
    const { participantId: _participantId, commandId: _commandId, ...rest } = activeTransfer;
    return rest;
  }, [activeTransfer]);
  // Room sound controls remote participants only. Muting every <audio> element
  // here also silences recordings, previews and radio players across the app.
  const { restoreApplicationAudio } = useApplicationAudioMute(false);
  const {
    localSpeakingLevel,
    speakingLevels,
    prepareSpeakingMeter,
    startSpeakingMeter,
    stopSpeakingMeter,
    stopAllSpeakingMeters
  } = useSpeakingLevels();
  const setRoom = useCallback(
    (next) => {
      roomRef.current = next;
      setRoomState(next);
    },
    // Stryker disable next-line ArrayDeclaration: React setters and refs are stable.
    []
  );
  const publishParticipantEffects = useCallback((settings) => {
    if (!roomRef.current || !settings) return false;
    return clientRef.current?.send("ui", {
      state: { participantEffects: normalizeParticipantEffects(settings) }
    });
  }, []);
  useEffect(() => {
    const publish = (event) => publishParticipantEffects(event.detail);
    globalThis.addEventListener?.(AUDIO_SETTINGS_CHANGED_EVENT, publish);
    return () => globalThis.removeEventListener?.(AUDIO_SETTINGS_CHANGED_EVENT, publish);
  }, [publishParticipantEffects]);
  const {
    applyParticipantEffects,
    applyRemoteAudioMute,
    attachRemoteStream,
    getRemoteVoiceStreams,
    removeAllRemoteAudio,
    removeRemoteAudio,
    setParticipantVolume: applyParticipantVolume,
    setLocalMonitoring,
    stopLocalMonitoring
  } = useOnlineRoomAudio({
    mutedPeopleRef,
    roomSoundMutedRef,
    roomUiRef,
    participantVolumesRef,
    startSpeakingMeter,
    stopSpeakingMeter,
    voiceRef
  });
  const estimateRemoteVoiceLatency = useCallback(
    () => voiceRef.current?.estimateInboundLatency?.() ?? Promise.resolve(0),
    []
  );
  const cleanupConnection = useCallback(
    () => {
      unsubscribeRef.current?.();
      unsubscribeRef.current = null;
      stopLocalMonitoring();
      voiceRef.current?.stop();
      stopAllSpeakingMeters();
      voiceRef.current = null;
      removeAllRemoteAudio();
      clientRef.current?.disconnect();
      clientRef.current = null;
    },
    // Stryker disable next-line ArrayDeclaration: both callbacks are stable.
    [removeAllRemoteAudio, stopAllSpeakingMeters, stopLocalMonitoring]
  );
  const setMicrophoneMuted = useCallback(
    (muted, broadcast = true) => {
      const next = Boolean(muted);
      voiceRef.current?.setMicrophoneMuted(next);
      microphoneMutedRef.current = next;
      setMicrophoneMutedState(next);
      if (broadcast) clientRef.current?.send("presence", { micMuted: next });
    },
    // Refs and React setters are stable.
    // Stryker disable next-line ArrayDeclaration
    []
  );
  const requestMicrophoneAccess = useCallback(
    async () => {
      const voice = voiceRef.current;
      if (!voice) {
        setVoiceError(translateSaved("room.firstConnectToTheRoom"));
        return false;
      }
      setVoiceError("");
      setTransferStatus(null);
      prepareSpeakingMeter();
      try {
        const stream = await voice.start();
        if (voiceRef.current !== voice) {
          stream.getTracks().forEach((track) => track.stop());
          return false;
        }
        startSpeakingMeter("local", voice.getMeterStream?.() || stream);
        const muted = microphoneMutedRef.current;
        voice.setMicrophoneMuted(muted);
        clientRef.current.send("presence", { micMuted: muted });
        return true;
      } catch (error) {
        if (voiceRef.current !== voice) return false;
        const message = getErrorMessage(error, translateSaved("room.noAccessToMicrophone"));
        setVoiceError(
          translateSaved("room.failedToAccessMicrophoneCheckYourWindowsResolutionAnd", {
            0: message
          })
        );
        return false;
      }
    },
    // Stryker disable next-line ArrayDeclaration: startSpeakingMeter is stable.
    [prepareSpeakingMeter, setTransferStatus, startSpeakingMeter]
  );
  useOnlineRoomHardwareSuspension({ requestMicrophoneAccess, stopSpeakingMeter, voiceRef });
  const requestSongSync = useCallback(
    (songId, ownerId, options) =>
      requestLibrarySong({
        songId,
        ownerId,
        options,
        voiceRef,
        roomRef,
        librarySyncRef,
        clientRef,
        setTransferStatus
      }),
    [setTransferStatus]
  );
  const setRoomSoundMuted = useCallback(
    (muted) => {
      const next = Boolean(muted);
      if (next === roomSoundMutedRef.current) return;
      roomSoundMutedRef.current = next;
      setRoomSoundMutedState(next);
      // Recover elements that an older app version may have left muted, then
      // let the room-audio layer apply the state only to participant streams.
      restoreApplicationAudio();
      // Room-output mute is local playback state only. Never disable the
      // outgoing microphone track: otherwise every participant loses this
      // user's voice and unmuting can race with WebRTC track state.
      applyRemoteAudioMute();
    },
    // Stryker disable next-line ArrayDeclaration: all callback dependencies are stable.
    [applyRemoteAudioMute, restoreApplicationAudio]
  );
  const resetRoomState = useCallback(
    () => {
      setRoom(null);
      setParticipants([]);
      mutedPeopleRef.current = new Set();
      setMutedPeople(new Set());
      setEffectPeople(new Set());
      participantVolumesRef.current = {};
      setParticipantVolumes({});
      roomSoundMutedRef.current = false;
      setRoomSoundMutedState(false);
      microphoneMutedRef.current = false;
      setMicrophoneMutedState(false);
      setRoomUi({});
      setRoomCommand(null);
      pendingSongCommandRef.current = null;
      hostSongCommandRef.current = null;
      songExportsRef.current.clear();
      roomTransferBroadcastRef.current = null;
      librarySyncRef.current?.reject?.(new Error(translateSaved("room.roomClosed")));
      librarySyncRef.current = null;
      setTransferStatus(null);
      setVoiceError("");
    },
    // Stryker disable next-line ArrayDeclaration: setRoom is stable.
    [setParticipants, setRoom, setTransferStatus]
  );
  const leaveRoom = useCallback(
    () => {
      const hadRoom = Boolean(roomRef.current);
      if (hadRoom && !roomSoundMutedRef.current) playParticipantLeftSound();
      connectionTokenRef.current = Symbol("left-room");
      intentionalDisconnectRef.current = true;
      restoreApplicationAudio();
      cleanupConnection();
      resetRoomState();
      intentionalDisconnectRef.current = false;
    },
    // Stryker disable next-line ArrayDeclaration: all callback dependencies are stable.
    [cleanupConnection, resetRoomState, restoreApplicationAudio]
  );
  const connect = useCallback(
    async ({ id, name, host, hostToken }) => {
      const connectionToken = Symbol("room-connection");
      connectionTokenRef.current = connectionToken;
      intentionalDisconnectRef.current = true;
      restoreApplicationAudio();
      cleanupConnection();
      intentionalDisconnectRef.current = false;
      resetRoomState();
      const client = new OnlineRoomClient();
      const voice = new OnlineVoiceMesh(client);
      clientRef.current = client;
      voiceRef.current = voice;
      const isCurrentConnection = () => connectionToken === connectionTokenRef.current;
      Object.assign(
        voice,
        createVoiceMeshHandlers({
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
        })
      );
      unsubscribeRef.current = client.onMessage(
        createOnlineRoomMessageHandler({
          id,
          client,
          voice,
          roomApi: api,
          isCurrentConnection,
          roomRef,
          participantsRef,
          intentionalDisconnectRef,
          pendingSongCommandRef,
          hostSongCommandRef,
          songExportsRef,
          cleanupConnection,
          setRoom,
          setParticipants,
          setRoomUi,
          setRoomCommand,
          setVoiceError,
          setTransferStatus,
          onParticipantJoined: (participant) => {
            if (!roomSoundMutedRef.current) playParticipantJoinedSound(participant);
            Promise.resolve(api.getAudioSettings?.())
              .then((settings) => settings && publishParticipantEffects(settings))
              .catch(() => {});
          },
          onParticipantLeft: () => {
            if (!roomSoundMutedRef.current) playParticipantLeftSound();
          },
          onEffectControl: (effects) => {
            api
              .updateAudioSettings(normalizeParticipantEffectPatch(effects))
              .then((updated) => {
                globalThis.dispatchEvent?.(
                  new CustomEvent(AUDIO_SETTINGS_CHANGED_EVENT, { detail: updated })
                );
              })
              .catch((error) => {
                if (isCurrentConnection())
                  setVoiceError(
                    translateSaved("room.couldNotApplyEffectSettings", {
                      0: getErrorMessage(error)
                    })
                  );
              });
          },
          onConnectionClosed: (message = translateSaved("room.theConnectionToTheRoomIsLost")) => {
            if (roomRef.current && !roomSoundMutedRef.current) playConnectionLostSound();
            connectionTokenRef.current = Symbol("connection-closed");
            restoreApplicationAudio();
            cleanupConnection();
            resetRoomState();
            setVoiceError(message);
          }
        })
      );
      prepareSpeakingMeter();
      try {
        const normalizedId = await client.connect({ id, name, host, hostToken });
        if (!isCurrentConnection())
          throw new Error(translateSaved("room.connectionCanceledByNewRequest"));
        if (!roomSoundMutedRef.current) playParticipantJoinedSound();

        // Show the room UI as soon as the WebSocket is connected. The server
        // room-state packet will replace the temporary self id a moment later.
        const pendingSelfId = `pending-${normalizedId}`;
        if (!roomRef.current?.selfId || roomRef.current.selfId.startsWith("pending-")) {
          setRoom({
            id: normalizedId,
            selfId: pendingSelfId,
            host: Boolean(host),
            role: host ? "host" : "guest"
          });
          setParticipants([
            {
              id: pendingSelfId,
              name: name?.trim() || translateSaved("room.guest"),
              role: host ? "host" : "guest",
              pending: true
            }
          ]);
        }
        Promise.resolve(api.getAudioSettings?.())
          .then((settings) => settings && publishParticipantEffects(settings))
          .catch(() => {});
        voice
          .start()
          .then((stream) => {
            if (!isCurrentConnection()) {
              stream.getTracks().forEach((track) => track.stop());
              return;
            }
            startSpeakingMeter("local", voice.getMeterStream?.() || stream);
            // Initial startup was bound just above. Future relay reconnects
            // can replace the MediaStream object without another connect(),
            // so keep the indicator attached to the current native source.
            voice.onLocalStream = (nextStream) => {
              if (isCurrentConnection()) startSpeakingMeter("local", nextStream);
            };
            voice.setMicrophoneMuted(microphoneMutedRef.current);
            client.send("presence", { micMuted: microphoneMutedRef.current });
          })
          .catch((error) => {
            if (!isCurrentConnection()) return;
            setVoiceError(
              translateSaved("room.roomConnectedWithoutVoice", {
                0: getErrorMessage(error, translateSaved("room.noAccessToMicrophone"))
              })
            );
          });
        return normalizedId;
      } catch (error) {
        if (isCurrentConnection()) cleanupConnection();
        else {
          voice.stop();
          client.disconnect();
        }
        throw error;
      }
    },
    // Stryker disable next-line ArrayDeclaration: all callback dependencies are stable.
    [
      attachRemoteStream,
      cleanupConnection,
      removeRemoteAudio,
      prepareSpeakingMeter,
      publishParticipantEffects,
      resetRoomState,
      restoreApplicationAudio,
      setParticipants,
      setRoom,
      setTransferStatus,
      startSpeakingMeter
    ]
  );
  useEffect(
    () => () => {
      restoreApplicationAudio();
      cleanupConnection();
    },
    [cleanupConnection, restoreApplicationAudio]
  );
  const createRoom = useCallback(
    (name) => connect({ id: createRoomId(), name, host: true, hostToken: createHostToken() }),
    // Stryker disable next-line ArrayDeclaration: connect is stable.
    [connect]
  );
  const joinRoom = useCallback(
    (id, name) => connect({ id, name, host: false }),
    // Stryker disable next-line ArrayDeclaration: connect is stable.
    [connect]
  );
  const {
    togglePersonMuted,
    setParticipantVolume,
    setEffectsLocked,
    requestParticipantEffects,
    togglePersonEffects
  } = useOnlineRoomParticipantControls({
    setMutedPeople,
    mutedPeopleRef,
    applyRemoteAudioMute,
    participantVolumesRef,
    setParticipantVolumes,
    applyParticipantVolume,
    clientRef,
    roomRef,
    setEffectPeople,
    applyParticipantEffects
  });
  const { effectsByParticipant } = roomUi;
  useEffect(() => {
    roomUiRef.current = roomUi;
  }, [roomUi]);
  useEffect(() => {
    // Tear down receive-side graphs left by an older renderer/hot reload.
    // Effects now run once on the sender and arrive as a normal WebRTC track.
    effectPeople.forEach((id) => applyParticipantEffects(id, false));
  }, [applyParticipantEffects, effectPeople, effectsByParticipant]);
  const { openKaraoke, roomClockNow, syncCommand, syncUi, getLocalVoiceStream } =
    useOnlineRoomCommands({
      api,
      clientRef,
      connectionTokenRef,
      hostSongCommandRef,
      onTransferStatus: setTransferStatus,
      participantsRef,
      roomRef,
      voiceRef
    });
  const value = useOnlineRoomValue({
    getLocalVoiceStream,
    createRoom,
    effectPeople,
    setEffectsLocked,
    joinRoom,
    leaveRoom,
    microphoneMuted,
    mutedPeople,
    openKaraoke,
    participants,
    participantVolumes,
    requestMicrophoneAccess,
    requestSongSync,
    requestParticipantEffects,
    roomClockNow,
    room,
    roomCommand,
    roomSoundMuted,
    roomUi,
    setLocalMonitoring,
    setMicrophoneMuted,
    setParticipantVolume,
    setRoomSoundMuted,
    syncCommand,
    syncUi,
    togglePersonEffects,
    togglePersonMuted,
    transferStatus,
    transferStatuses,
    estimateRemoteVoiceLatency,
    getRemoteVoiceStreams,
    voiceError,
    voiceRef
  });
  const speakingValue = useMemo(
    () => ({ localSpeakingLevel, speakingLevels }),
    [localSpeakingLevel, speakingLevels]
  );
  return (
    <OnlineRoomContext.Provider value={value}>
      <OnlineRoomSpeakingContext.Provider value={speakingValue}>
        {children}
      </OnlineRoomSpeakingContext.Provider>
    </OnlineRoomContext.Provider>
  );
}
export function useOnlineRoom() {
  return useContext(OnlineRoomContext);
}
export function useOnlineRoomSpeaking() {
  return useContext(OnlineRoomSpeakingContext);
}
