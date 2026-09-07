import { api } from "../api/client";
// eslint-disable-next-line import/extensions
import { translateSaved } from "../i18n/runtime";
import { closeAudioContext, closeAudioContextQuietly } from "../utils/audio-context";
import { MICROPHONE_CAPTURE_CONSTRAINTS } from "../utils/microphone-capture-constraints";
import { resolveMicrophoneDevice } from "./microphoneDevice";
import { createStudioMicrophoneGraph } from "./microphoneStudioQuality";
import { suspendVoiceMicrophone } from "./onlineVoiceHardwareLifecycle";
import { createRelayVoiceGraph } from "./pythonVoiceRelay";
import { updatePeerIceServers } from "./onlineVoicePeerConfiguration";
import OnlineVoicePeerRecovery from "./onlineVoicePeerRecovery";
// Audio is peer-to-peer; the signaling Worker never stores microphone data.

import OnlineVoiceTransferSession from "./onlineVoiceTransferSession";

const MAX_PENDING_ICE_CANDIDATES = 256;
const OPUS_PACKET_TIME_MS = 5;

export function preferLowLatencyOpus(description) {
  const sdp = description?.sdp;
  if (typeof sdp !== "string" || !sdp) return description;
  const separator = sdp.includes("\r\n") ? "\r\n" : "\n";
  const sections = sdp.split(/(?=^m=)/m);
  const audioIndex = sections.findIndex((section) => section.startsWith("m=audio"));
  if (audioIndex < 0) return description;
  const opus = sections[audioIndex].match(/^a=rtpmap:(\d+) opus\/48000(?:\/\d+)?\s*$/im);
  if (!opus) return description;

  const payload = opus[1];
  const lines = sections[audioIndex].split(/\r?\n/);
  const mediaLine = lines.shift();
  const filtered = lines.filter((line) => !/^a=(?:p|maxp)time:/i.test(line));
  const fmtpIndex = filtered.findIndex((line) =>
    new RegExp(`^a=fmtp:${payload}(?:\\s|$)`, "i").test(line)
  );
  if (fmtpIndex >= 0) {
    const [prefix, parameters = ""] = filtered[fmtpIndex].split(/\s+/, 2);
    const values = parameters
      .split(";")
      .map((value) => value.trim())
      .filter(Boolean)
      .filter(
        (value) => !/^(?:minptime|usedtx|stereo|sprop-stereo|maxaveragebitrate|cbr)=/i.test(value)
      );
    filtered[fmtpIndex] =
      `${prefix} minptime=${OPUS_PACKET_TIME_MS};usedtx=0;stereo=0;sprop-stereo=0;maxaveragebitrate=128000;cbr=1;${values.join(";")}`.replace(
        /;$/,
        ""
      );
  }
  filtered.unshift(`a=maxptime:${OPUS_PACKET_TIME_MS}`);
  filtered.unshift(`a=ptime:${OPUS_PACKET_TIME_MS}`);
  sections[audioIndex] = [mediaLine, ...filtered].join(separator);
  return { type: description.type, sdp: sections.join("") };
}

export default class OnlineVoiceMesh {
  constructor(roomClient) {
    this.roomClient = roomClient;
    this.peers = new Map();
    this.pendingCandidates = new Map();
    this.pendingInvites = new Set();
    this.invitePromises = new Map();
    this.signalPromises = new Map();
    this.peerVersions = new Map();
    this.peerInitiators = new Set();
    this.recovery = new OnlineVoicePeerRecovery(this, preferLowLatencyOpus);
    this.recoveryPromises = this.recovery.promises;
    this.transfers = new OnlineVoiceTransferSession(
      {
        version: () => this.lifecycleVersion,
        hasPeer: (id) => this.peers.has(id),
        invite: (id) => this.invite(id)
      },
      {
        canAcceptFile: (...args) => this.canAcceptFile?.(...args),
        onFile: (...args) => this.onFile?.(...args),
        onSongPullRequest: (...args) => this.onSongPullRequest?.(...args),
        onSongPullError: (...args) => this.onSongPullError?.(...args),
        onTransferProgress: (...args) => this.onTransferProgress?.(...args)
      }
    );
    this.stream = null;
    this.effectsStream = null;
    this.peerEffectsEnabled = new Map();
    this.microphoneGraph = null;
    // True when microphoneGraph came from the Python monitor's audio relay
    // (see pythonVoiceRelay.js) instead of the local getUserMedia + Web
    // Audio DSP graph -- read by callers that need to tell the backend
    // which DSP source is live (see body.voice_relay in /recording/start).
    this.usingRelay = false;
    this.outputDeviceId = "";
    this.startPromise = null;
    this.lifecycleVersion = 0;
    this.onRemoteStream = null;
    this.onPeerClosed = null;
    this.canAcceptFile = null;
    this.onFile = null;
    this.onSongPullRequest = null;
    this.onSongPullError = null;
    this.onTransferProgress = null;
    this.disconnectTimers = new Map();
    this.connectTimers = new Map();
    this.onPeerError = null;
    this.onPeerRecovering = null;
    this.onPeerRecovered = null;
    this.iceServers = [{ urls: "stun:stun.cloudflare.com:3478" }];
  }

  async start() {
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.getUserMedia) {
      throw new Error(translateSaved("room.microphoneCaptureIsNotSupportedInThisEnvironment"));
    }
    // Checked before any await below: two calls to start() landing in the
    // same tick (e.g. two components each mounting and calling voice.start()
    // on room join) must not both pass the stale-stream cleanup below and
    // race to close/null the same microphoneGraph.
    if (this.startPromise) return this.startPromise;
    const liveStream = this.stream?.getAudioTracks?.().some((track) => track.readyState === "live");
    if (liveStream) return this.stream;
    if (this.stream) {
      if (this.microphoneGraph) {
        await closeAudioContext(this.microphoneGraph);
        this.microphoneGraph = null;
      } else this.stream.getTracks?.().forEach((track) => track.stop());
      this.stream = null;
    }
    const { lifecycleVersion } = this;
    const cancelled = () => lifecycleVersion !== this.lifecycleVersion;
    let capturedStream = null;
    let ownedGraph = null;
    let persistedSettings;
    const syncPeersAndInvite = async () => {
      for (const [participantId, peer] of this.peers) {
        await this.syncPeerAudioTrack(participantId, peer);
        await this.optimizeAudioSenders(peer);
        this.pendingInvites.add(participantId);
      }
      const pending = [...this.pendingInvites];
      this.pendingInvites.clear();
      await Promise.allSettled(pending.map((participantId) => this.invite(participantId)));
    };
    // Shared by both DSP sources (Python relay and local Web Audio graph):
    // wires up the graph's outgoing streams and re-syncs any already-
    // connected peers to it. Committing microphoneGraph here (rather than
    // before this point) lets setSinkId below act on the graph that is
    // actually about to be used.
    const finishGraph = async (graph) => {
      ownedGraph = graph;
      this.microphoneGraph = graph;
      if (cancelled()) throw new Error(translateSaved("room.microphoneLaunchCanceled"));
      await this.setSinkId(this.outputDeviceId);
      if (cancelled()) throw new Error(translateSaved("room.microphoneLaunchCanceled"));
      const outgoingStream = graph.stream;
      this.stream = outgoingStream;
      this.effectsStream = graph.effectsStream || outgoingStream;
      outgoingStream.getAudioTracks().forEach((track) => {
        track.contentHint = "music";
      });
      this.effectsStream.getAudioTracks().forEach((track) => {
        track.contentHint = "music";
      });
      await syncPeersAndInvite();
      return outgoingStream;
    };
    // Tries the Python monitor's audio relay first, so the room reuses the
    // same DSP the solo monitor already runs instead of a second JS
    // implementation of it. Returns null (never throws, except on
    // cancellation) so the caller falls back to the local Web Audio graph
    // when the relay isn't available -- an unsupported/failed ASIO driver,
    // no backend monitor running, or a connection timeout.
    const tryRelay = async (settings) => {
      if (settings?.audio_driver === "asio") return null;
      // The backend only ever opens this relay on request -- previously
      // that request was voice_relay=true on /recording/start, but the
      // frontend only ever sent that once it already knew the relay
      // worked (this.usingRelay), which was only ever set by successfully
      // connecting to a relay that first required voice_relay=true to
      // exist. That self-referential requirement meant the relay could
      // never open on a cold room join. Ask for it explicitly first.
      let prepared;
      try {
        prepared = await api.prepareRoomVoiceRelay();
      } catch {
        return null;
      }
      if (cancelled()) throw new Error(translateSaved("room.microphoneLaunchCanceled"));
      if (!prepared?.relay_available) return null;
      let graph;
      try {
        graph = await createRelayVoiceGraph({ connectTimeoutMs: 1500 });
      } catch {
        return null;
      }
      if (cancelled()) {
        await graph.close();
        throw new Error(translateSaved("room.microphoneLaunchCanceled"));
      }
      this.usingRelay = true;
      graph.onUnavailable(() => this.handleRelayDropped());
      return finishGraph(graph);
    };
    const startPromise = api
      .getAudioSettings()
      .catch(() => null)
      .then(async (settings) => {
        persistedSettings = settings;
        const relayStream = await tryRelay(settings);
        if (relayStream) return relayStream;
        this.usingRelay = false;
        const deviceId = await resolveMicrophoneDevice(settings);
        if (cancelled()) throw new Error(translateSaved("room.microphoneLaunchCanceled"));
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: {
            ...MICROPHONE_CAPTURE_CONSTRAINTS,
            // Browser AEC adds a capture look-ahead/buffer that is audible in
            // realtime self-monitoring on consumer USB headsets. The studio
            // graph already provides our own noise gate and dynamics, so keep
            // Chromium's latency-heavy voice processing out of the duet path.
            echoCancellation: false,
            channelCount: 1,
            latency: { ideal: 0 },
            sampleRate: { ideal: 48_000 },
            ...(deviceId && { deviceId: { exact: deviceId } })
          }
        });
        capturedStream = stream;
        if (cancelled()) {
          stream.getTracks().forEach((track) => track.stop());
          throw new Error(translateSaved("room.microphoneLaunchCanceled"));
        }
        // The graph otherwise defaults to a stale/hardcoded noise-suppression
        // level until some settings save happens to dispatch
        // AUDIO_SETTINGS_CHANGED_EVENT -- read the persisted value up front so a
        // room call actually starts with whatever the user last saved.
        const graph = createStudioMicrophoneGraph(stream, {
          noiseSuppression: persistedSettings?.noise_suppression,
          octave: persistedSettings?.octave,
          volume: persistedSettings?.volume,
          reverb: persistedSettings?.reverb,
          echo: persistedSettings?.echo,
          delay: persistedSettings?.delay
        });
        return finishGraph(graph);
      })
      .catch(async (error) => {
        if (ownedGraph && this.microphoneGraph === ownedGraph) {
          await closeAudioContext(this.microphoneGraph);
          this.microphoneGraph = null;
          this.stream = null;
        } else capturedStream?.getTracks?.().forEach((track) => track.stop());
        throw error;
      })
      .finally(() => {
        if (this.startPromise === startPromise) this.startPromise = null;
      });
    this.startPromise = startPromise;
    return startPromise;
  }

  // Called when the relay WebSocket drops mid-call (network blip, or the
  // backend monitor stopped) -- rebuilds the local Web Audio DSP graph and
  // re-syncs already-connected peers to it, rather than leaving them with a
  // dead track. Guarded by usingRelay so onclose/onerror firing together
  // cannot trigger this twice.
  async handleRelayDropped() {
    if (!this.usingRelay) return;
    this.usingRelay = false;
    const previousGraph = this.microphoneGraph;
    this.microphoneGraph = null;
    this.stream = null;
    this.effectsStream = null;
    try {
      await this.start();
    } catch (error) {
      console.error("Could not rebuild the voice graph after the microphone relay dropped", error);
    } finally {
      if (previousGraph) await previousGraph.close().catch(() => {});
    }
  }

  getMeterStream() { return this.microphoneGraph?.rawStream || this.stream; }

  getOutgoingStream(participantId) {
    return this.peerEffectsEnabled.get(participantId) && this.effectsStream
      ? this.effectsStream
      : this.stream;
  }

  async syncPeerAudioTrack(participantId, peer = this.peers.get(participantId)) {
    const selectedStream = this.getOutgoingStream(participantId);
    const selectedTracks = selectedStream?.getAudioTracks?.() || [];
    if (!peer || !selectedTracks.length) return false;
    const senders = peer.getSenders?.().filter((item) => item.track?.kind === "audio") || [];
    if (selectedTracks.length === 1 && senders.length === 1) {
      const [selectedTrack] = selectedTracks;
      const [sender] = senders;
      if (sender.track === selectedTrack) return true;
      if (typeof sender.replaceTrack === "function") {
        await sender.replaceTrack(selectedTrack);
        return true;
      }
    }
    const selectedIds = new Set(selectedTracks.map((track) => track.id));
    const existingIds = new Set(senders.map((sender) => sender.track?.id).filter(Boolean));
    let changed = false;
    // Drop a sender whose track is no longer part of the selected stream
    // instead of leaving it be -- a reconnect/toggle race could previously
    // leave more than one sender behind (e.g. one still carrying the dry
    // track, one added later for the wet track), and the old code only ever
    // added a missing track, never dropped an unwanted extra one, so a
    // stray sender just kept sending stale audio to the peer indefinitely.
    for (const sender of senders) {
      if (sender.track && !selectedIds.has(sender.track.id) && typeof peer.removeTrack === "function") {
        try {
          peer.removeTrack(sender);
          changed = true;
        } catch {
          // Already removed, or the connection is closing.
        }
      }
    }
    selectedTracks.forEach((track) => {
      if (existingIds.has(track.id)) return;
      peer.addTrack(track, selectedStream);
      changed = true;
    });
    return changed;
  }

  async setPeerEffectsEnabled(participantId, enabled) {
    this.peerEffectsEnabled.set(participantId, Boolean(enabled));
    return this.syncPeerAudioTrack(participantId);
  }

  async setLocalMonitoring(enabled, effects = {}) {
    if (!enabled) return this.microphoneGraph?.setMonitoring?.(false) ?? false;
    await this.start();
    return this.microphoneGraph?.setMonitoring?.(true, effects) ?? false;
  }

  async setSinkId(deviceId) {
    this.outputDeviceId = typeof deviceId === "string" ? deviceId : "";
    const context = this.microphoneGraph?.context;
    if (typeof context?.setSinkId !== "function") return false;
    try {
      await context.setSinkId(this.outputDeviceId);
      return true;
    } catch {
      return false;
    }
  }

  createPeer(participantId) {
    if (typeof participantId !== "string" || !participantId || participantId.length > 128) {
      throw new TypeError(translateSaved("room.invalidMemberId"));
    }
    if (typeof globalThis.RTCPeerConnection !== "function") {
      throw new Error(translateSaved("room.webrtcIsNotSupportedInThisEnvironment"));
    }
    const current = this.peers.get(participantId);
    if (current) return current;
    const peer = new globalThis.RTCPeerConnection({
      iceServers: this.iceServers,
      bundlePolicy: "max-bundle",
      rtcpMuxPolicy: "require",
      iceCandidatePoolSize: 4
    });
    const outgoingStream = this.getOutgoingStream(participantId);
    if (outgoingStream) {
      outgoingStream.getTracks().forEach((track) => {
        peer.addTrack(track, outgoingStream);
      });
      // The microphone was already running when this peer joined, so unlike
      // the peers start() itself walks and optimizes when the mic first
      // comes up, nothing has applied bitrate/priority encoding params to
      // this brand-new peer's senders yet.
      this.optimizeAudioSenders(peer);
    }
    const isCurrentPeer = () => this.peers.get(participantId) === peer;
    peer.onicecandidate = ({ candidate }) => {
      if (!candidate || !isCurrentPeer()) return;
      this.roomClient.send("signal", { targetId: participantId, signal: { candidate } });
    };
    peer.ontrack = ({ receiver, streams }) => {
      const stream = streams[0];
      if (!stream) return;
      if (!isCurrentPeer()) {
        stream.getTracks?.().forEach((track) => track.stop());
        return;
      }
      // Singing is more sensitive to delay than speech. Chromium still
      // clamps these hints when the network needs a larger safety buffer.
      for (const [property, value] of [
        ["jitterBufferTarget", 0],
        ["playoutDelayHint", 0]
      ]) {
        try {
          if (property in (receiver || {})) receiver[property] = value;
        } catch {
          // Older WebRTC implementations may expose a read-only hint.
        }
      }
      this.onRemoteStream?.(participantId, stream);
    };
    peer.ondatachannel = ({ channel }) => {
      if (!isCurrentPeer()) {
        channel.close?.();
        return;
      }
      this.setupDataChannel(participantId, channel);
    };
    peer.oniceconnectionstatechange = () => {
      if (!isCurrentPeer()) return;
      console.info("WebRTC ICE state changed", {
        participantId,
        iceConnectionState: peer.iceConnectionState
      });
    };
    peer.onconnectionstatechange = () => {
      if (!isCurrentPeer()) return;
      console.info("WebRTC connection state changed", {
        participantId,
        connectionState: peer.connectionState
      });
      const previousTimer = this.disconnectTimers.get(participantId);
      if (previousTimer) {
        globalThis.clearTimeout(previousTimer);
        this.disconnectTimers.delete(participantId);
      }
      if (["failed", "closed"].includes(peer.connectionState)) {
        if (peer.connectionState === "closed") this.removePeer(participantId);
        else this.recoverPeer(participantId, peer);
        return;
      }
      if (peer.connectionState === "disconnected") {
        const timer = globalThis.setTimeout(() => {
          this.disconnectTimers.delete(participantId);
          if (isCurrentPeer() && peer.connectionState === "disconnected") {
            this.recoverPeer(participantId, peer);
          }
        }, 10_000);
        this.disconnectTimers.set(participantId, timer);
      }
      if (peer.connectionState === "connected") {
        this.recovery.connected(participantId);
        const connectTimer = this.connectTimers.get(participantId);
        if (connectTimer) globalThis.clearTimeout(connectTimer);
        this.connectTimers.delete(participantId);
      }
    };
    this.peers.set(participantId, peer);
    this.connectTimers.set(
      participantId,
      globalThis.setTimeout(() => {
        this.connectTimers.delete(participantId);
        if (isCurrentPeer() && peer.connectionState !== "connected") {
          this.recovery.fail(participantId);
          this.removePeer(participantId);
        }
      }, 30_000)
    );
    return peer;
  }

  recoverPeer(participantId, peer) {
    return this.recovery.recover(participantId, peer);
  }

  async optimizeAudioSenders(peer) {
    await Promise.allSettled(
      peer
        .getSenders()
        .filter((sender) => sender.track?.kind === "audio" && sender.setParameters)
        .map(async (sender) => {
          const parameters = sender.getParameters();
          const encodings = parameters.encodings?.length ? parameters.encodings : [{}];
          parameters.encodings = encodings.map((encoding) => ({
            ...encoding,
            maxBitrate: 128_000,
            priority: "high",
            networkPriority: "high"
          }));
          parameters.degradationPreference = "maintain-framerate";
          await sender.setParameters(parameters);
        })
    );
  }

  async getInboundLatencyDiagnostics() {
    return Promise.all(
      [...this.peers.entries()].map(async ([participantId, peer]) => {
        const empty = {
          participantId,
          jitterMs: 0,
          jitterBufferMs: 0,
          minimumPlayoutMs: 0,
          networkOneWayMs: 0,
          estimatedTotalMs: 0,
          packetsLost: 0,
          concealedSamples: 0
        };
        if (typeof peer.getStats !== "function") return empty;
        try {
          const stats = await peer.getStats();
          const result = { ...empty };
          let roundTrip = 0;
          stats.forEach((report) => {
            if (report.type === "inbound-rtp" && report.kind === "audio") {
              const emitted = Number(report.jitterBufferEmittedCount);
              const total = Number(report.jitterBufferDelay);
              const buffered = emitted > 0 && Number.isFinite(total) ? total / emitted : 0;
              const minimumTotal = Number(report.jitterBufferMinimumDelay);
              result.jitterBufferMs = Math.max(result.jitterBufferMs, buffered * 1000);
              result.minimumPlayoutMs = Math.max(
                result.minimumPlayoutMs,
                emitted > 0 && Number.isFinite(minimumTotal) ? (minimumTotal / emitted) * 1000 : 0
              );
              result.jitterMs = Math.max(result.jitterMs, (Number(report.jitter) || 0) * 1000);
              result.packetsLost = Math.max(result.packetsLost, Number(report.packetsLost) || 0);
              result.concealedSamples = Math.max(
                result.concealedSamples,
                Number(report.concealedSamples) || 0
              );
            }
            if (
              report.type === "candidate-pair" &&
              (report.selected || report.nominated) &&
              report.state === "succeeded"
            ) {
              roundTrip = Math.max(roundTrip, Number(report.currentRoundTripTime) || 0);
            }
          });
          result.networkOneWayMs = (roundTrip * 1000) / 2;
          result.estimatedTotalMs = Math.max(
            0,
            Math.min(500, Math.max(result.jitterBufferMs, result.jitterMs) + result.networkOneWayMs)
          );
          return result;
        } catch {
          return empty;
        }
      })
    );
  }

  async estimateInboundLatency() {
    const diagnostics = await this.getInboundLatencyDiagnostics();
    if (diagnostics.length) console.info("WebRTC singing latency", diagnostics);
    const estimates = diagnostics.map(({ estimatedTotalMs }) => estimatedTotalMs / 1000);
    return estimates.length ? Math.max(...estimates) : 0;
  }

  async invite(participantId) {
    if (!participantId) return false;
    const version = this.lifecycleVersion;
    if (this.roomClient.getIceServers) this.iceServers = await this.roomClient.getIceServers();
    if (version !== this.lifecycleVersion) return false;
    const pendingInvite = this.invitePromises.get(participantId);
    if (pendingInvite) return pendingInvite;
    const { lifecycleVersion } = this;
    this.peerInitiators.add(participantId);
    const peer = this.createPeer(participantId);
    const isCurrentPeer = () =>
      lifecycleVersion === this.lifecycleVersion &&
      this.peers.get(participantId) === peer &&
      peer.connectionState !== "closed";
    const invitePromise = (async () => {
      if (!this.transfers.hasChannel(participantId)) {
        this.setupDataChannel(
          participantId,
          peer.createDataChannel("karaoke-library", { ordered: true })
        );
      }
      try {
        const offer = preferLowLatencyOpus(await peer.createOffer());
        if (!isCurrentPeer()) return false;
        await peer.setLocalDescription(offer);
        if (!isCurrentPeer() || !peer.localDescription) return false;
        return this.roomClient.send("signal", {
          targetId: participantId,
          signal: { description: peer.localDescription }
        });
      } catch (error) {
        if (!isCurrentPeer()) return false;
        throw error;
      }
    })().finally(() => {
      if (this.invitePromises.get(participantId) === invitePromise)
        this.invitePromises.delete(participantId);
    });
    this.invitePromises.set(participantId, invitePromise);
    return invitePromise;
  }

  async accept(fromId, signal) {
    if (
      typeof fromId !== "string" ||
      !fromId ||
      fromId.length > 128 ||
      !signal ||
      typeof signal !== "object" ||
      Array.isArray(signal)
    ) {
      return false;
    }
    if (typeof signal.effectsEnabled === "boolean") {
      await this.setPeerEffectsEnabled(fromId, signal.effectsEnabled);
      return true;
    }
    const peerVersion = this.peerVersions.get(fromId) || 0;
    const previousSignal = this.signalPromises.get(fromId) || Promise.resolve();
    const signalPromise = previousSignal
      .catch(() => {})
      .then(async () => {
        if ((this.peerVersions.get(fromId) || 0) !== peerVersion) return false;
        const { lifecycleVersion } = this;
        if (this.roomClient.getIceServers) this.iceServers = await this.roomClient.getIceServers();
        if (
          lifecycleVersion !== this.lifecycleVersion ||
          (this.peerVersions.get(fromId) || 0) !== peerVersion
        )
          return false;
        const peer = this.createPeer(fromId);
        updatePeerIceServers(peer, this.iceServers);
        const isCurrentPeer = () =>
          lifecycleVersion === this.lifecycleVersion &&
          (this.peerVersions.get(fromId) || 0) === peerVersion &&
          this.peers.get(fromId) === peer &&
          peer.connectionState !== "closed";
        if (signal.candidate) {
          if (!isCurrentPeer()) return false;
          if (peer.remoteDescription) {
            await peer.addIceCandidate(signal.candidate);
            return isCurrentPeer();
          }
          const queue = this.pendingCandidates.get(fromId) || [];
          if (queue.length >= MAX_PENDING_ICE_CANDIDATES) {
            this.removePeer(fromId);
            throw new Error(translateSaved("room.tooManyIceCandidatesReceived"));
          }
          queue.push(signal.candidate);
          this.pendingCandidates.set(fromId, queue);
          return true;
        }
        if (!signal.description) return false;
        if (`${signal.description.type}:${peer.signalingState}` === "offer:have-local-offer")
          await peer.setLocalDescription({ type: "rollback" }); // glare: roll back our own offer first
        await peer.setRemoteDescription(signal.description);
        if (!isCurrentPeer()) return false;
        const candidates = this.pendingCandidates.get(fromId) || [];
        this.pendingCandidates.delete(fromId);
        // ICE candidates must be applied in arrival order.
        // eslint-disable-next-line no-restricted-syntax
        for (const candidate of candidates) {
          if (!isCurrentPeer()) return false;
          // eslint-disable-next-line no-await-in-loop
          await peer.addIceCandidate(candidate);
        }
        if (signal.description.type !== "offer") return true;
        const answer = preferLowLatencyOpus(await peer.createAnswer());
        if (!isCurrentPeer()) return false;
        await peer.setLocalDescription(answer);
        if (!isCurrentPeer() || !peer.localDescription) return false;
        return this.roomClient.send("signal", {
          targetId: fromId,
          signal: { description: peer.localDescription }
        });
      })
      .finally(() => {
        if (this.signalPromises.get(fromId) === signalPromise) this.signalPromises.delete(fromId);
      });
    this.signalPromises.set(fromId, signalPromise);
    return signalPromise;
  }

  setMicrophoneMuted(muted) {
    const tracks = new Set([
      ...(this.stream?.getAudioTracks?.() || []),
      ...(this.effectsStream?.getAudioTracks?.() || [])
    ]);
    tracks.forEach((track) => {
      track.enabled = !muted;
    });
  }

  suspendMicrophone() { return suspendVoiceMicrophone(this); }

  setupDataChannel(...args) { return this.transfers.setupDataChannel(...args); }

  waitForDataChannel(...args) {
    return this.transfers.waitForDataChannel(...args);
  }

  sendFile(...args) {
    return this.transfers.sendFile(...args);
  }

  sendSongSyncError(...args) {
    return this.transfers.sendSongSyncError(...args);
  }

  cancelTransfersByCommandId(...args) {
    return this.transfers.cancelTransfersByCommandId(...args);
  }

  removePeer(participantId) {
    const connectTimer = this.connectTimers.get(participantId);
    if (connectTimer) globalThis.clearTimeout(connectTimer);
    this.connectTimers.delete(participantId);
    const disconnectTimer = this.disconnectTimers.get(participantId);
    if (disconnectTimer) globalThis.clearTimeout(disconnectTimer);
    this.disconnectTimers.delete(participantId);
    const existed = this.peers.has(participantId) || this.transfers.hasChannel(participantId);
    this.peerVersions.set(participantId, (this.peerVersions.get(participantId) || 0) + 1);
    const peer = this.peers.get(participantId);
    this.peers.delete(participantId);
    peer?.close();
    this.pendingCandidates.delete(participantId);
    this.pendingInvites.delete(participantId);
    this.invitePromises.delete(participantId);
    this.signalPromises.delete(participantId);
    this.peerInitiators.delete(participantId);
    this.recovery.remove(participantId);
    this.peerEffectsEnabled.delete(participantId);
    this.transfers.removePeer(participantId);
    if (existed) this.onPeerClosed?.(participantId);
  }

  stop() {
    this.lifecycleVersion += 1;
    new Set([...this.peers.keys(), ...this.transfers.participantIds()]).forEach((id) =>
      this.removePeer(id)
    );
    this.transfers.stop();
    if (this.microphoneGraph) {
      closeAudioContextQuietly(this.microphoneGraph);
      this.microphoneGraph = null;
    } else this.stream?.getTracks().forEach((track) => track.stop());
    // Counterpart to tryRelay's prepareRoomVoiceRelay -- releases the
    // backend relay (falls back to ordinary monitoring) once this room's
    // local voice capture stops, whether or not a recording ever actually
    // used it. Best-effort and fire-and-forget: stop() itself stays
    // synchronous, matching closeAudioContextQuietly just above.
    if (this.usingRelay) api.releaseRoomVoiceRelay().catch(() => {});
    this.usingRelay = false;
    this.stream = null;
    this.effectsStream = null;
    this.startPromise = null;
    this.pendingInvites.clear();
    this.invitePromises.clear();
    this.signalPromises.clear();
    this.peerInitiators.clear();
    this.recovery.stop();
    this.peerEffectsEnabled.clear();
    for (const timer of this.disconnectTimers.values()) {
      globalThis.clearTimeout(timer);
    }
    this.disconnectTimers.clear();
  }
}
