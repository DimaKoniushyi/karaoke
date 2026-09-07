import { createFileUrl, encodePathSegment, request } from "../core";
import { normalizeRecording } from "../normalizers";

function sessionQuery(sessionId, extra = {}) {
  const params = { session_id: sessionId ?? "", ...extra };
  return `?${Object.entries(params)
    .map(([key, value]) => `${key}=${encodeURIComponent(String(value))}`)
    .join("&")}`;
}

export const recordingsApi = {
  getRecordingSettings: () => request("/recording/settings"),
  // Opens the Python monitor's audio relay ahead of the room actually
  // recording anything -- must be called (and awaited) before attempting to
  // connect to it (see onlineVoiceMesh.js's tryRelay), or the relay never
  // has a reason to exist yet.
  prepareRoomVoiceRelay: () => request("/recording/room/prepare-voice-relay", { method: "POST" }),
  releaseRoomVoiceRelay: () => request("/recording/room/release-voice-relay", { method: "POST" }),
  startRecording: (
    songId,
    positionSec = 0,
    musicVolume = 1,
    microphoneVolume = 1,
    reverb = 0,
    echo = 0,
    delay = 0,
    roomMode = false,
    octave = 0,
    playbackRate = 1,
    voiceRelay = false
  ) =>
    request("/recording/start", {
      method: "POST",
      body: JSON.stringify({
        song_id: songId,
        position_sec: positionSec,
        music_volume: musicVolume,
        microphone_volume: microphoneVolume,
        reverb,
        echo,
        delay,
        octave,
        playback_rate: playbackRate,
        room_mode: roomMode,
        voice_relay: voiceRelay
      })
    }),
  pauseRecording: (sessionId) =>
    request(`/recording/pause${sessionQuery(sessionId)}`, { method: "POST" }),
  resumeRecording: (sessionId) =>
    request(`/recording/resume${sessionQuery(sessionId)}`, { method: "POST" }),
  syncRecording: (sessionId, positionSec, playbackRate = 1) =>
    request(
      `/recording/sync${sessionQuery(sessionId, { position_sec: positionSec ?? 0, playback_rate: playbackRate ?? 1 })}`,
      { method: "POST" }
    ),
  updateRecordingControls: (sessionId, controls) =>
    request(`/recording/controls${sessionQuery(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify({
        music_volume: controls.musicVolume,
        microphone_volume: controls.microphoneVolume,
        reverb: controls.reverb,
        echo: controls.echo,
        delay: controls.delay,
        octave: controls.octave
      })
    }),
  stopRecording: (sessionId) =>
    request(`/recording/stop${sessionQuery(sessionId)}`, { method: "POST" }),
  attachRoomAudio: (recordingId, blob, startPlaybackSec = 0, latencyCompensationSec = 0) => {
    const form = new FormData();
    form.append("file", blob, "room-voices.webm");
    form.append("start_playback_sec", String(startPlaybackSec));
    form.append("latency_compensation_sec", String(latencyCompensationSec));
    return request(`/recording/${encodePathSegment(recordingId)}/room-audio`, {
      method: "POST",
      body: form,
      timeoutMs: 5 * 60_000
    });
  },
  listRecordingsForSong: (songId) =>
    request(`/recording/by-song/${encodePathSegment(songId)}`).then((items) =>
      Array.isArray(items) ? items.map(normalizeRecording) : []
    ),
  listRecordingLibrary: () =>
    request("/recording/library").then((items) =>
      Array.isArray(items) ? items.map(normalizeRecording) : []
    ),
  deleteRecording: (id) => request(`/recording/${encodePathSegment(id)}`, { method: "DELETE" }),
  getRecordingFileUrl: (id) => createFileUrl(`/recording/${encodePathSegment(id)}/file`),
  getPerformanceFileUrl: (id) => createFileUrl(`/recording/${encodePathSegment(id)}/performance`),
  runAnalysis: (recordingId) =>
    request(`/analysis/${encodePathSegment(recordingId)}/run`, {
      method: "POST",
      timeoutMs: 10 * 60_000
    }),
  getAnalysis: (recordingId) => request(`/analysis/${encodePathSegment(recordingId)}`)
};
