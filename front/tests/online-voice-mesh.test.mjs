import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { FakeChannel, FakePeer, stream, track } from "./helpers/webrtc.mjs";

const relayMocks = vi.hoisted(() => ({ createRelayVoiceGraph: vi.fn() }));
vi.mock("../src/services/pythonVoiceRelay.js", () => ({
  createRelayVoiceGraph: relayMocks.createRelayVoiceGraph
}));

function fakeRelayGraph({ dryStream = stream([track("relay-dry")]), wetStream = stream([track("relay-wet")]) } = {}) {
  let unavailableCallback = null;
  return {
    stream: dryStream,
    effectsStream: wetStream,
    rawStream: dryStream,
    context: {},
    setNoiseSuppression: vi.fn(),
    setPitchShift: vi.fn(),
    setEffects: vi.fn(() => false),
    setMonitoring: vi.fn(() => false),
    getStream: () => dryStream,
    getEffectsStream: () => wetStream,
    onUnavailable: vi.fn((callback) => {
      unavailableCallback = callback;
    }),
    close: vi.fn().mockResolvedValue(undefined),
    triggerUnavailable: () => unavailableCallback?.()
  };
}

let OnlineVoiceMesh;
let OnlineVoiceTransferSession;
let preferLowLatencyOpus;
const makeMesh = () => new OnlineVoiceMesh({ send: vi.fn(() => true) });
const setupChannel = (peer = "guest") => {
  const mesh = makeMesh();
  const channel = new FakeChannel();
  mesh.setupDataChannel(peer, channel);
  return { mesh, channel };
};
beforeEach(async () => {
  vi.resetModules();
  relayMocks.createRelayVoiceGraph.mockReset().mockRejectedValue(new Error("relay not available in tests by default"));
  ({ default: OnlineVoiceMesh, preferLowLatencyOpus } = await import("../src/services/onlineVoiceMesh.js"));
  ({ default: OnlineVoiceTransferSession } = await import("../src/services/onlineVoiceTransferSession.js"));
  FakePeer.instances = [];
  globalThis.RTCPeerConnection = FakePeer;
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  delete globalThis.RTCPeerConnection;
  if (globalThis.navigator) {
    delete globalThis.navigator.mediaDevices;
    delete globalThis.navigator.storage;
  }
});
describe("online voice mesh", () => {
  test("routes the existing microphone graph without another capture or context", async () => {
    const mesh = makeMesh();
    await expect(mesh.setSinkId("headphones")).resolves.toBe(false);
    expect(mesh.outputDeviceId).toBe("headphones");
    const setSinkId = vi.fn().mockResolvedValue(undefined);
    mesh.microphoneGraph = { context: { setSinkId } };
    await expect(mesh.setSinkId(mesh.outputDeviceId)).resolves.toBe(true);
    expect(setSinkId).toHaveBeenCalledWith("headphones");
    setSinkId.mockRejectedValueOnce(new Error("device unavailable"));
    await expect(mesh.setSinkId("missing")).resolves.toBe(false);
    await expect(mesh.setSinkId("")).resolves.toBe(true);
    expect(setSinkId).toHaveBeenLastCalledWith("");
  });
  test("requests five-millisecond Opus packets without changing unrelated media sections", () => {
    const input = {
      type: "offer",
      sdp: [
        "v=0",
        "m=audio 9 UDP/TLS/RTP/SAVPF 111",
        "a=rtpmap:111 opus/48000/2",
        "a=fmtp:111 minptime=20;useinbandfec=1",
        "a=ptime:20",
        "m=video 9 UDP/TLS/RTP/SAVPF 96",
        "a=rtpmap:96 VP8/90000",
        ""
      ].join("\r\n")
    };
    const result = preferLowLatencyOpus(input);
    expect(result).not.toBe(input);
    expect(result.sdp).toContain("a=ptime:5\r\na=maxptime:5");
    expect(result.sdp).toContain("a=fmtp:111 minptime=5;usedtx=0;stereo=0;sprop-stereo=0;maxaveragebitrate=128000;cbr=1;useinbandfec=1");
    expect(result.sdp).not.toContain("a=ptime:20");
    expect(result.sdp).toContain("m=video 9 UDP/TLS/RTP/SAVPF 96");
    expect(preferLowLatencyOpus({ type: "offer", sdp: "v=0\r\nm=video 9" })).toEqual({
      type: "offer",
      sdp: "v=0\r\nm=video 9"
    });
  });
  test("uses the raw microphone stream for the visual level meter", () => {
    const mesh = makeMesh();
    const processedStream = { id: "processed" };
    const rawStream = { id: "raw" };
    mesh.stream = processedStream;
    expect(mesh.getMeterStream()).toBe(processedStream);
    mesh.microphoneGraph = { rawStream };
    expect(mesh.getMeterStream()).toBe(rawStream);
  });
  test("monitors inside the existing microphone graph without another capture", async () => {
    const mesh = makeMesh();
    const live = stream([track("live", "live")]);
    const setMonitoring = vi.fn((enabled) => Boolean(enabled));
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia: vi.fn() } }
    });
    mesh.stream = live;
    mesh.microphoneGraph = { rawStream: live, setMonitoring };
    await expect(mesh.setLocalMonitoring(true, { volume: 0.8, reverb: 0.2, echo: 0.1, delay: 0 })).resolves.toBe(true);
    expect(setMonitoring).toHaveBeenCalledWith(true, {
      volume: 0.8,
      reverb: 0.2,
      echo: 0.1,
      delay: 0
    });
    await expect(mesh.setLocalMonitoring(false)).resolves.toBe(false);
    expect(setMonitoring).toHaveBeenLastCalledWith(false);
  });

  test("switches each peer between synchronized dry and effected sender tracks", async () => {
    const mesh = makeMesh();
    const dryTrack = track("dry");
    const wetTrack = track("wet");
    mesh.stream = stream([dryTrack]);
    mesh.effectsStream = stream([wetTrack]);
    const peer = mesh.createPeer("guest");
    const sender = peer.getSenders()[0];
    sender.replaceTrack = vi.fn(async (nextTrack) => {
      sender.track = nextTrack;
    });

    await expect(mesh.accept("guest", { effectsEnabled: true })).resolves.toBe(true);
    expect(sender.replaceTrack).toHaveBeenLastCalledWith(wetTrack);
    expect(mesh.peerEffectsEnabled.get("guest")).toBe(true);
    await expect(mesh.accept("guest", { effectsEnabled: false })).resolves.toBe(true);
    expect(sender.replaceTrack).toHaveBeenLastCalledWith(dryTrack);
    expect(mesh.peerEffectsEnabled.get("guest")).toBe(false);

    mesh.setMicrophoneMuted(true);
    expect(dryTrack.enabled).toBe(false);
    expect(wetTrack.enabled).toBe(false);
  });

  test("reports network and receiver buffering separately for duet latency diagnostics", async () => {
    const mesh = makeMesh();
    const peer = mesh.createPeer("guest");
    peer.getStats = vi.fn().mockResolvedValue(
      new Map([
        [
          "inbound",
          {
            type: "inbound-rtp",
            kind: "audio",
            jitter: 0.004,
            jitterBufferDelay: 0.3,
            jitterBufferMinimumDelay: 0.2,
            jitterBufferEmittedCount: 10,
            packetsLost: 2,
            concealedSamples: 48
          }
        ],
        [
          "pair",
          {
            type: "candidate-pair",
            selected: true,
            state: "succeeded",
            currentRoundTripTime: 0.04
          }
        ]
      ])
    );
    await expect(mesh.getInboundLatencyDiagnostics()).resolves.toEqual([
      {
        participantId: "guest",
        jitterMs: 4,
        jitterBufferMs: 30,
        minimumPlayoutMs: 20,
        networkOneWayMs: 20,
        estimatedTotalMs: 50,
        packetsLost: 2,
        concealedSamples: 48
      }
    ]);
    await expect(mesh.estimateInboundLatency()).resolves.toBeCloseTo(0.05);
  });

  test("uses the exact capture, peer and sender quality contracts", async () => {
    const media = stream([track("voice"), { ...track("video"), kind: "video" }]);
    const capture = vi.fn().mockResolvedValue(media);
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia: capture } }
    });
    const mesh = makeMesh();
    await mesh.start();
    expect(capture).toHaveBeenCalledWith({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
        channelCount: 1,
        latency: { ideal: 0 },
        sampleRate: { ideal: 48_000 }
      }
    });
    expect(media.getAudioTracks()[0].contentHint).toBe("music");
    const peer = mesh.createPeer("p".repeat(128));
    expect(peer.configuration).toEqual({
      iceServers: [{ urls: "stun:stun.cloudflare.com:3478" }],
      bundlePolicy: "max-bundle",
      rtcpMuxPolicy: "require",
      iceCandidatePoolSize: 4
    });
    for (const participantId of [null, 1, {}, "", "p".repeat(129)]) {
      expect(() => mesh.createPeer(participantId)).toThrow("Некорректный идентификатор участника");
    }
    const configured = {
      track: track(),
      getParameters: () => ({ encodings: [{ active: true }] }),
      setParameters: vi.fn()
    };
    const noAudio = {
      track: { ...track(), kind: "video" },
      getParameters: vi.fn(),
      setParameters: vi.fn()
    };
    const noSetter = { track: track(), getParameters: vi.fn() };
    await mesh.optimizeAudioSenders({ getSenders: () => [configured, noAudio, noSetter] });
    expect(configured.setParameters).toHaveBeenCalledWith({
      encodings: [{ active: true, maxBitrate: 128_000, priority: "high", networkPriority: "high" }],
      degradationPreference: "maintain-framerate"
    });
    expect(noAudio.getParameters).not.toHaveBeenCalled();
    expect(noSetter.getParameters).not.toHaveBeenCalled();
  });
  test("optimizes audio senders only for a peer created after the microphone is already running", async () => {
    const freshMesh = makeMesh();
    const earlySpy = vi.spyOn(freshMesh, "optimizeAudioSenders");
    freshMesh.createPeer("early-joiner");
    expect(earlySpy).not.toHaveBeenCalled();
    class AudioSenderPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.addTrack = vi.fn((mediaTrack) => {
          const sender = {
            track: mediaTrack,
            getParameters: () => ({ encodings: [{}] }),
            setParameters: vi.fn().mockResolvedValue(undefined)
          };
          this.senders.push(sender);
          return sender;
        });
      }
    }
    const media = stream([track("mic")]);
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(media) } }
    });
    const mesh = makeMesh();
    await mesh.start();
    const lateSpy = vi.spyOn(mesh, "optimizeAudioSenders");
    // No peer existed before the microphone started, so nothing was created
    // (and therefore nothing optimized) via the start() loop for this peer.
    globalThis.RTCPeerConnection = AudioSenderPeer;
    const lateJoiner = mesh.createPeer("late-joiner");
    expect(lateSpy).toHaveBeenCalledWith(lateJoiner);
    const sender = lateJoiner.getSenders()[0];
    await vi.waitFor(() => expect(sender.setParameters).toHaveBeenCalled());
    expect(sender.setParameters).toHaveBeenCalledWith({
      encodings: [{ maxBitrate: 128_000, priority: "high", networkPriority: "high" }],
      degradationPreference: "maintain-framerate"
    });
  });
  test("validates exact public error and data-channel creation contracts", async () => {
    const mesh = makeMesh();
    delete globalThis.RTCPeerConnection;
    expect(() => mesh.createPeer("guest")).toThrow("WebRTC не поддерживается в этом окружении");
    globalThis.RTCPeerConnection = FakePeer;
    const invitation = mesh.invite("guest");
    const peer = FakePeer.instances.at(-1);
    expect(peer.createDataChannel).toHaveBeenCalledWith("karaoke-library", { ordered: true });
    await invitation;
    expect(await mesh.accept("x".repeat(128), {})).toBe(false);
    expect(await mesh.accept("x".repeat(129), {})).toBe(false);
    expect(await mesh.accept(1, {})).toBe(false);
    expect(await mesh.accept("guest", null)).toBe(false);
    expect(await mesh.accept("guest", "signal")).toBe(false);
  });
  test("preserves typed-array boundaries and exact incoming metadata limits", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    let received;
    mesh.onFile = vi.fn((_participantId, blob, metadata) => {
      received = { blob, metadata };
      return true;
    });
    mesh.setupDataChannel("host", channel);
    expect(channel.binaryType).toBe("arraybuffer");
    expect(channel.bufferedAmountLowThreshold).toBe(256 * 1024);
    const backing = new Uint8Array([9, 1, 2, 9]);
    channel.onmessage({
      data: JSON.stringify({
        type: "file-start",
        transferId: "t".repeat(128),
        size: 2,
        kind: "k".repeat(65),
        songId: "s".repeat(129),
        filename: "f".repeat(513),
        mimeType: "m".repeat(256)
      })
    });
    channel.onmessage({ data: backing.subarray(1, 3) });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "t".repeat(128) }) });
    await vi.waitFor(() => expect(received).toBeTruthy());
    expect([...new Uint8Array(await received.blob.arrayBuffer())]).toEqual([1, 2]);
    expect(received.metadata).toEqual({
      type: "file-start",
      kind: "k".repeat(64),
      songId: "s".repeat(128),
      size: 2,
      transferId: "t".repeat(128),
      filename: "f".repeat(512),
      mimeType: "m".repeat(255)
    });
  });
  test("streams incoming packages to OPFS instead of buffering payload chunks", async () => {
    const writes = [];
    const removeEntry = vi.fn().mockResolvedValue(undefined);
    const writable = {
      write: vi.fn(async (chunk) => writes.push(...new Uint8Array(chunk))),
      close: vi.fn().mockResolvedValue(undefined),
      abort: vi.fn().mockResolvedValue(undefined)
    };
    globalThis.navigator.storage = {
      getDirectory: vi.fn().mockResolvedValue({
        getFileHandle: vi.fn().mockResolvedValue({
          createWritable: vi.fn().mockResolvedValue(writable),
          getFile: vi.fn().mockResolvedValue(new Blob([[1, 2]]))
        }),
        removeEntry
      })
    };
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.onFile = vi.fn().mockResolvedValue(true);
    mesh.setupDataChannel("host", channel);
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "disk", size: 2 })
    });
    await vi.waitFor(() => expect(mesh.transfers.incomingFiles.has("host")).toBe(true));
    channel.onmessage({ data: new Uint8Array([1, 2]).buffer });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "disk" }) });
    await vi.waitFor(() => expect(mesh.onFile).toHaveBeenCalledOnce());
    expect(writes).toEqual([1, 2]);
    expect(writable.close).toHaveBeenCalledOnce();
    await vi.waitFor(() => expect(removeEntry).toHaveBeenCalledOnce());
  });
  test("sends exact bounded metadata, ordered chunks and progress", async () => {
    const { mesh, channel } = setupChannel();
    mesh.onTransferProgress = vi.fn();
    const metadata = { kind: "k".repeat(65), songId: "s".repeat(129), filename: "f".repeat(513) };
    const payload = new Blob([new Uint8Array(40_000)], { type: "application/zip" });
    const sending = mesh.sendFile("guest", payload, metadata);
    await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
    const transferId = [...mesh.transfers.pendingTransferConfirmations.keys()][0];
    const sent = channel.send.mock.calls.map(([value]) => value);
    expect(JSON.parse(sent[0])).toEqual({
      type: "file-start",
      transferId,
      size: 40_000,
      kind: "k".repeat(64),
      songId: "s".repeat(128),
      filename: "f".repeat(512),
      mimeType: "application/zip"
    });
    expect(sent.slice(1, 3).map(({ byteLength }) => byteLength)).toEqual([32 * 1024, 40_000 - 32 * 1024]);
    expect(JSON.parse(sent[3])).toEqual({ type: "file-end", transferId });
    expect(mesh.onTransferProgress.mock.calls.map(([event]) => event)).toEqual([
      { participantId: "guest", stage: "sending", percent: 0, metadata },
      { participantId: "guest", stage: "sending", percent: 81, metadata },
      { participantId: "guest", stage: "sending", percent: 99, metadata }
    ]);
    channel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId }) });
    await sending;
    expect(mesh.onTransferProgress).toHaveBeenLastCalledWith({
      participantId: "guest",
      stage: "complete",
      percent: 100,
      metadata
    });
  });
  test("resumes a song transfer after a disconnect without resending confirmed chunks", async () => {
    const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: {
        subtle: {
          digest: vi.fn(async (_algorithm, data) => {
            const bytes = ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : new Uint8Array(data);
            const hash = new Uint8Array(32);
            bytes.forEach((value, index) => {
              hash[index % hash.length] = (hash[index % hash.length] + value + index) & 0xff;
            });
            return hash.buffer;
          })
        }
      }
    });
    let sender;
    let receiver;
    let generation = 0;
    let interrupted = false;
    const sentBinaryBytes = [];
    const resumeOffsets = [];
    const closePair = (left, right) => {
      if (left.readyState !== "open") return;
      left.readyState = "closed";
      right.readyState = "closed";
      left.onclose?.();
      right.onclose?.();
    };
    const connect = () => {
      const index = generation;
      generation += 1;
      sentBinaryBytes[index] = 0;
      const left = { readyState: "open", bufferedAmount: 0, label: "song" };
      const right = { readyState: "open", bufferedAmount: 0, label: "song" };
      left.close = vi.fn(() => closePair(left, right));
      right.close = vi.fn(() => closePair(left, right));
      left.send = vi.fn((data) => {
        if (data instanceof ArrayBuffer) sentBinaryBytes[index] += data.byteLength;
        right.onmessage?.({ data });
      });
      right.send = vi.fn((data) => {
        if (typeof data === "string") {
          const message = JSON.parse(data);
          if (message.type === "file-ready" && index > 0) resumeOffsets.push(message.resumeOffset);
        }
        left.onmessage?.({ data });
      });
      sender.setupDataChannel("receiver", left);
      receiver.setupDataChannel("sender", right);
      return { left, right };
    };
    const senderConnection = {
      version: () => 1,
      hasPeer: () => true,
      invite: vi.fn(async () => {
        connect();
        return true;
      })
    };
    const receiverConnection = { version: () => 1, hasPeer: () => true, invite: vi.fn() };
    let received;
    sender = new OnlineVoiceTransferSession(senderConnection, {});
    receiver = new OnlineVoiceTransferSession(receiverConnection, {
      canAcceptFile: () => true,
      onFile: async (_participantId, file) => {
        received = file;
        return true;
      },
      onTransferProgress: ({ stage, percent }) => {
        if (!interrupted && stage === "receiving" && percent >= 90) {
          interrupted = true;
          const left = sender.channels.get("receiver");
          const right = receiver.channels.get("sender");
          closePair(left, right);
        }
      }
    });
    connect();
    const bytes = new Uint8Array(20 * 32 * 1024);
    bytes.forEach((_value, index) => {
      bytes[index] = index % 251;
    });
    const payload = new Blob([bytes], { type: "application/zip" });
    try {
      await sender.sendFile("receiver", payload, {
        resumable: true,
        kind: "song-package",
        songId: "song",
        revision: "revision"
      });
      expect(generation).toBe(2);
      expect(resumeOffsets[0]).toBeGreaterThan(0);
      expect(sentBinaryBytes[1]).toBe(payload.size - resumeOffsets[0]);
      expect(new Uint8Array(await received.arrayBuffer())).toEqual(bytes);
    } finally {
      sender.stop();
      receiver.stop();
      if (cryptoDescriptor) Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
      else delete globalThis.crypto;
    }
  });
  test("re-requests a corrupted song chunk and verifies the final hash manifest", async () => {
    const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: {
        subtle: {
          digest: vi.fn(async (_algorithm, data) => {
            const bytes = ArrayBuffer.isView(data) ? new Uint8Array(data.buffer, data.byteOffset, data.byteLength) : new Uint8Array(data);
            const hash = new Uint8Array(32);
            bytes.forEach((value, index) => {
              hash[index % hash.length] = (hash[index % hash.length] + value + index) & 0xff;
            });
            return hash.buffer;
          })
        }
      }
    });
    const senderConnection = { version: () => 1, hasPeer: () => true, invite: vi.fn() };
    const receiverConnection = { version: () => 1, hasPeer: () => true, invite: vi.fn() };
    let received;
    const sender = new OnlineVoiceTransferSession(senderConnection, {});
    const receiver = new OnlineVoiceTransferSession(receiverConnection, {
      canAcceptFile: () => true,
      onFile: async (_participantId, file) => {
        received = file;
        return true;
      }
    });
    const left = { readyState: "open", bufferedAmount: 0, label: "song" };
    const right = { readyState: "open", bufferedAmount: 0, label: "song" };
    let binaryIndex = 0;
    let corrupted = false;
    let starts = 0;
    left.close = vi.fn(() => {
      left.readyState = "closed";
    });
    right.close = vi.fn(() => {
      right.readyState = "closed";
    });
    left.send = vi.fn((data) => {
      if (typeof data === "string" && JSON.parse(data).type === "file-start") starts += 1;
      let delivered = data;
      if (data instanceof ArrayBuffer) {
        binaryIndex += 1;
        if (!corrupted && binaryIndex === 2) {
          const copy = new Uint8Array(data.slice(0));
          copy[0] ^= 0xff;
          delivered = copy.buffer;
          corrupted = true;
        }
      }
      right.onmessage?.({ data: delivered });
    });
    right.send = vi.fn((data) => left.onmessage?.({ data }));
    sender.setupDataChannel("receiver", left);
    receiver.setupDataChannel("sender", right);
    const bytes = new Uint8Array(3 * 32 * 1024);
    bytes.forEach((_value, index) => {
      bytes[index] = index % 239;
    });
    try {
      await sender.sendFile("receiver", new Blob([bytes]), {
        resumable: true,
        kind: "song-package",
        songId: "song",
        revision: "revision"
      });
      expect(corrupted).toBe(true);
      expect(starts).toBe(2);
      expect(new Uint8Array(await received.arrayBuffer())).toEqual(bytes);
    } finally {
      sender.stop();
      receiver.stop();
      if (cryptoDescriptor) Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
      else delete globalThis.crypto;
    }
  });
  test("deletes resumable partial storage on expiry and explicit cancellation", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const startPartial = (transferId) => {
      const channel = new FakeChannel();
      mesh.setupDataChannel("host", channel);
      channel.onmessage({
        data: JSON.stringify({
          type: "file-start",
          transferId,
          size: 100,
          chunkSize: 32 * 1024
        })
      });
      const transfer = mesh.transfers.incomingFiles.get("host");
      channel.readyState = "closed";
      channel.onclose();
      return transfer;
    };

    const expired = startPartial("expires");
    expect(mesh.transfers.resumableIncomingFiles.get("host")).toBe(expired);
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    expect(mesh.transfers.resumableIncomingFiles.has("host")).toBe(false);
    expect(expired.sink.chunks).toEqual([]);

    const cancelled = startPartial("cancelled");
    const control = new FakeChannel();
    mesh.setupDataChannel("host", control);
    control.onmessage({
      data: JSON.stringify({ type: "file-cancel", transferId: "cancelled" })
    });
    expect(mesh.transfers.resumableIncomingFiles.has("host")).toBe(false);
    expect(cancelled.sink.chunks).toEqual([]);
    mesh.stop();
  });
  test("disposes a stale resumable slot immediately when a new file-start supersedes it", async () => {
    // A fresh file-start for a participant clearly supersedes whatever
    // paused resume slot is sitting there. Without disposing it right away
    // it used to leak its open sink until its own TTL fired, up to 10
    // minutes later.
    vi.useFakeTimers();
    const mesh = makeMesh();
    const startPartial = (transferId) => {
      const channel = new FakeChannel();
      mesh.setupDataChannel("host", channel);
      channel.onmessage({
        data: JSON.stringify({ type: "file-start", transferId, size: 100, chunkSize: 32 * 1024 })
      });
      const transfer = mesh.transfers.incomingFiles.get("host");
      channel.readyState = "closed";
      channel.onclose();
      return transfer;
    };

    const stale = startPartial("old-transfer");
    expect(mesh.transfers.resumableIncomingFiles.get("host")).toBe(stale);

    const fresh = new FakeChannel();
    mesh.setupDataChannel("host", fresh);
    fresh.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "new-transfer", size: 200, chunkSize: 32 * 1024 })
    });

    expect(mesh.transfers.resumableIncomingFiles.has("host")).toBe(false);
    expect(stale.sink.chunks).toEqual([]);
    expect(mesh.transfers.incomingFiles.get("host").metadata.transferId).toBe("new-transfer");
    mesh.stop();
  });
  test("normalizes missing outbound metadata and validates every input boundary", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.setupDataChannel("g".repeat(128), channel);
    for (const participantId of [null, 1, {}, "", "g".repeat(129)]) {
      await expect(mesh.sendFile(participantId, new Blob([]))).rejects.toThrow("Для передачи нужны участник и файл");
    }
    await expect(mesh.sendFile("guest", {})).rejects.toThrow("Для передачи нужны участник и файл");
    const oversized = Object.create(Blob.prototype);
    Object.defineProperties(oversized, {
      size: { value: 512 * 1024 * 1024 + 1 },
      type: { value: "" }
    });
    await expect(mesh.sendFile("guest", oversized)).rejects.toThrow("Файл слишком большой для передачи через комнату");
    const sending = mesh.sendFile("g".repeat(128), new Blob([]));
    await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
    const transferId = [...mesh.transfers.pendingTransferConfirmations.keys()][0];
    expect(JSON.parse(channel.send.mock.calls[0][0])).toEqual({
      type: "file-start",
      transferId,
      size: 0,
      mimeType: "application/octet-stream"
    });
    channel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId }) });
    await sending;
  });
  test("reports the exact incoming lifecycle and protocol errors", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.onTransferProgress = vi.fn();
    mesh.setupDataChannel("host", channel);
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size: 4 }) });
    channel.onmessage({ data: new Uint8Array([1, 2]) });
    channel.onmessage({ data: new Uint8Array([3, 4]) });
    expect(mesh.onTransferProgress.mock.calls.map(([event]) => event)).toEqual([
      {
        participantId: "host",
        stage: "receiving",
        percent: 0,
        metadata: {
          type: "file-start",
          kind: undefined,
          songId: undefined,
          size: 4,
          transferId: "t",
          filename: undefined,
          mimeType: "application/octet-stream"
        }
      },
      expect.objectContaining({ stage: "receiving", percent: 50 }),
      expect.objectContaining({ stage: "receiving", percent: 99 })
    ]);
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "wrong" }) });
    expect(JSON.parse(channel.send.mock.calls.at(-1)[0])).toEqual({
      type: "file-error",
      transferId: "wrong",
      error: "Получен неизвестный идентификатор передачи"
    });
    channel.onmessage({ data: JSON.stringify({ type: "file-cancel", transferId: "t" }) });
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "stall", size: 1 })
    });
    vi.advanceTimersByTime(29_999);
    expect(mesh.transfers.incomingFiles.has("host")).toBe(true);
    vi.advanceTimersByTime(1);
    expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
    expect(JSON.parse(channel.send.mock.calls.at(-1)[0])).toEqual({
      type: "file-error",
      transferId: "stall",
      error: "Передача песни остановилась"
    });
    expect(mesh.onTransferProgress).toHaveBeenLastCalledWith(
      expect.objectContaining({ participantId: "host", stage: "error", percent: 0 })
    );
  });
  test("distinguishes unavailable, stale, live and structurally partial captures", async () => {
    const mesh = makeMesh();
    for (const value of [undefined, {}, { mediaDevices: {} }]) {
      if (value === undefined) delete globalThis.navigator;
      else Object.defineProperty(globalThis, "navigator", { configurable: true, value });
      await expect(mesh.start()).rejects.toThrow("Захват микрофона не поддерживается в этом окружении");
    }
    const capture = vi.fn().mockResolvedValue(stream([track("fresh")]));
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia: capture } }
    });
    const stopped = track("stopped", "ended");
    mesh.stream = { getAudioTracks: () => [stopped], getTracks: () => [stopped] };
    await mesh.start();
    expect(stopped.stop).toHaveBeenCalled();
    expect(capture).toHaveBeenCalledOnce();
    const live = stream([track("live", "live")]);
    mesh.stream = live;
    expect(await mesh.start()).toBe(live);
    expect(capture).toHaveBeenCalledOnce();
    const partial = { getAudioTracks: undefined, getTracks: undefined };
    mesh.stream = partial;
    await mesh.start();
    expect(capture).toHaveBeenCalledTimes(2);
  });
  test("ignores stale peer events and handles every connection transition exactly", () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    mesh.onRemoteStream = vi.fn();
    mesh.onPeerClosed = vi.fn();
    const peer = mesh.createPeer("guest");
    const stale = { ...track("remote"), stop: vi.fn() };
    const staleStream = stream([stale]);
    mesh.peers.delete("guest");
    peer.onicecandidate({ candidate: { value: "stale" } });
    peer.ontrack({ streams: [staleStream] });
    const staleChannel = new FakeChannel();
    peer.ondatachannel({ channel: staleChannel });
    expect(mesh.roomClient.send).not.toHaveBeenCalled();
    expect(stale.stop).toHaveBeenCalledOnce();
    expect(staleChannel.close).toHaveBeenCalledOnce();
    expect(mesh.onRemoteStream).not.toHaveBeenCalled();
    const current = mesh.createPeer("current");
    const remote = stream([track("current-remote")]);
    current.ontrack({ streams: [] });
    const receiver = { jitterBufferTarget: null, playoutDelayHint: null };
    current.ontrack({ receiver, streams: [remote] });
    expect(receiver).toMatchObject({ jitterBufferTarget: 0, playoutDelayHint: 0 });
    expect(mesh.onRemoteStream).toHaveBeenCalledWith("current", remote);
    current.onicecandidate({ candidate: null });
    expect(mesh.roomClient.send).not.toHaveBeenCalled();
    current.connectionState = "disconnected";
    current.onconnectionstatechange();
    const timer = mesh.disconnectTimers.get("current");
    expect(timer).toBeTruthy();
    current.connectionState = "connected";
    current.onconnectionstatechange();
    expect(mesh.disconnectTimers.has("current")).toBe(false);
    current.connectionState = "closed";
    current.onconnectionstatechange();
    expect(mesh.peers.has("current")).toBe(false);
    expect(mesh.onPeerClosed).toHaveBeenCalledWith("current");
  });
  test("keeps peer removal idempotent and stop invalidates every resource", () => {
    const mesh = makeMesh();
    mesh.onPeerClosed = vi.fn();
    const peerOnly = mesh.createPeer("peer");
    const channelOnly = new FakeChannel();
    mesh.transfers.channels.set("channel", channelOnly);
    const local = track("local");
    mesh.stream = stream([local]);
    mesh.pendingCandidates.set("peer", [1]);
    mesh.pendingInvites.add("peer");
    mesh.invitePromises.set("peer", Promise.resolve());
    mesh.signalPromises.set("peer", Promise.resolve());
    const version = mesh.lifecycleVersion;
    mesh.stop();
    expect(mesh.lifecycleVersion).toBe(version + 1);
    expect(peerOnly.close).toHaveBeenCalledOnce();
    expect(channelOnly.close).toHaveBeenCalledOnce();
    expect(local.stop).toHaveBeenCalledOnce();
    expect(mesh.onPeerClosed.mock.calls.map(([id]) => id).sort()).toEqual(["channel", "peer"]);
    for (const collection of [
      mesh.peers,
      mesh.transfers.channels,
      mesh.pendingCandidates,
      mesh.pendingInvites,
      mesh.invitePromises,
      mesh.signalPromises,
      mesh.transfers.incomingFiles,
      mesh.disconnectTimers
    ]) {
      expect(collection.size).toBe(0);
    }
    mesh.removePeer("missing");
    expect(mesh.onPeerClosed).toHaveBeenCalledTimes(2);
    expect(mesh.peerVersions.get("missing")).toBe(1);
    mesh.removePeer("missing");
    expect(mesh.peerVersions.get("missing")).toBe(2);
  });
  test.each(["lifecycle", "replacement", "closed"])("cancels an invitation when only its %s invariant changes", async (invariant) => {
    let release;
    globalThis.RTCPeerConnection = class DeferredOfferPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.createOffer = vi.fn(
          () =>
            new Promise((resolve) => {
              release = resolve;
            })
        );
      }
    };
    const mesh = makeMesh();
    const invitation = mesh.invite("guest");
    const peer = FakePeer.instances.at(-1);
    if (invariant === "lifecycle") mesh.lifecycleVersion += 1;
    if (invariant === "replacement") mesh.peers.set("guest", {});
    if (invariant === "closed") peer.connectionState = "closed";
    release({ type: "offer", sdp: "offer" });
    await expect(invitation).resolves.toBe(false);
    expect(peer.setLocalDescription).not.toHaveBeenCalled();
    expect(mesh.roomClient.send).not.toHaveBeenCalled();
    expect(mesh.invitePromises.has("guest")).toBe(false);
  });
  test.each(["lifecycle", "replacement", "closed", "missing-local"])(
    "cancels an invitation after local description when %s changes",
    async (invariant) => {
      let release;
      globalThis.RTCPeerConnection = class DeferredLocalPeer extends FakePeer {
        constructor(configuration) {
          super(configuration);
          this.setLocalDescription = vi.fn(
            (description) =>
              new Promise((resolve) => {
                this.localDescription = invariant === "missing-local" ? null : description;
                release = resolve;
              })
          );
        }
      };
      const mesh = makeMesh();
      const invitation = mesh.invite("guest");
      await vi.waitFor(() => expect(release).toBeTypeOf("function"));
      const peer = FakePeer.instances.at(-1);
      if (invariant === "lifecycle") mesh.lifecycleVersion += 1;
      if (invariant === "replacement") mesh.peers.set("guest", {});
      if (invariant === "closed") peer.connectionState = "closed";
      release();
      await expect(invitation).resolves.toBe(false);
      expect(mesh.roomClient.send).not.toHaveBeenCalled();
    }
  );
  test.each(["lifecycle", "version", "replacement", "closed"])(
    "cancels description processing when only its %s invariant changes",
    async (invariant) => {
      let release;
      globalThis.RTCPeerConnection = class DeferredDescriptionPeer extends FakePeer {
        constructor(configuration) {
          super(configuration);
          this.setRemoteDescription = vi.fn(
            (description) =>
              new Promise((resolve) => {
                this.remoteDescription = description;
                release = resolve;
              })
          );
        }
      };
      const mesh = makeMesh();
      const accepted = mesh.accept("guest", { description: { type: "offer", sdp: "offer" } });
      await vi.waitFor(() => expect(release).toBeTypeOf("function"));
      const peer = FakePeer.instances.at(-1);
      if (invariant === "lifecycle") mesh.lifecycleVersion += 1;
      if (invariant === "version") mesh.peerVersions.set("guest", 1);
      if (invariant === "replacement") mesh.peers.set("guest", {});
      if (invariant === "closed") peer.connectionState = "closed";
      release();
      await expect(accepted).resolves.toBe(false);
      expect(peer.createAnswer).not.toHaveBeenCalled();
      expect(mesh.roomClient.send).not.toHaveBeenCalled();
      expect(mesh.signalPromises.has("guest")).toBe(false);
    }
  );
  test("distinguishes candidates, answers and offers without cross-work", async () => {
    const mesh = makeMesh();
    expect(await mesh.accept("guest", { candidate: "ice" })).toBe(true);
    const peer = FakePeer.instances.at(-1);
    expect(mesh.pendingCandidates.get("guest")).toEqual(["ice"]);
    expect(peer.addIceCandidate).not.toHaveBeenCalled();
    expect(peer.setRemoteDescription).not.toHaveBeenCalled();
    expect(await mesh.accept("guest", { description: { type: "answer", sdp: "answer" } })).toBe(true);
    expect(peer.setRemoteDescription).toHaveBeenCalledWith({ type: "answer", sdp: "answer" });
    expect(peer.addIceCandidate).toHaveBeenCalledWith("ice");
    expect(peer.createAnswer).not.toHaveBeenCalled();
    expect(mesh.pendingCandidates.has("guest")).toBe(false);
  });
  test("keeps immutable peer configuration while rotating ICE servers", async () => {
    globalThis.RTCPeerConnection = class StrictConfigurationPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.getConfiguration = vi.fn(() => ({ ...this.configuration }));
        this.setConfiguration = vi.fn((next) => {
          for (const field of ["bundlePolicy", "rtcpMuxPolicy", "iceCandidatePoolSize"]) {
            if (next[field] !== this.configuration[field]) {
              throw new DOMException(
                "Attempted to modify the PeerConnection's configuration in an unsupported way.",
                "InvalidModificationError"
              );
            }
          }
          this.configuration = { ...next };
        });
      }
    };
    const mesh = makeMesh();
    await expect(mesh.invite("guest")).resolves.toBe(true);

    await expect(mesh.accept("guest", { candidate: "ice" })).resolves.toBe(true);

    const peer = FakePeer.instances.at(-1);
    expect(peer.setConfiguration).toHaveBeenCalledWith({
      iceServers: mesh.iceServers,
      bundlePolicy: "max-bundle",
      rtcpMuxPolicy: "require",
      iceCandidatePoolSize: 4
    });
    const channel = peer.createDataChannel.mock.results[0].value;
    const sending = mesh.sendFile("guest", new Blob(["song-package"]), { songId: "song" });
    await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
    const transferId = [...mesh.transfers.pendingTransferConfirmations.keys()][0];
    channel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId }) });
    await expect(sending).resolves.toBeUndefined();
  });
  test("rolls back its own outgoing offer to accept a simultaneous incoming offer (glare)", async () => {
    // Both sides can end up mid-offer to each other at once (e.g. both
    // reconnect from a shared network blip and both send a fresh invite).
    // Without yielding, an incoming offer while in have-local-offer throws
    // InvalidStateError and the peer never negotiates.
    const mesh = makeMesh();
    const peer = mesh.createPeer("guest");
    peer.signalingState = "have-local-offer";
    const order = [];
    peer.setLocalDescription = vi.fn(async (description) => {
      order.push(["setLocalDescription", description.type]);
      peer.localDescription = description;
      if (description.type === "rollback") peer.signalingState = "stable";
    });
    peer.setRemoteDescription = vi.fn(async (description) => {
      order.push(["setRemoteDescription", description.type]);
      peer.remoteDescription = description;
    });

    const accepted = await mesh.accept("guest", {
      description: { type: "offer", sdp: "their-offer" }
    });

    expect(accepted).toBe(true);
    expect(order.slice(0, 2)).toEqual([
      ["setLocalDescription", "rollback"],
      ["setRemoteDescription", "offer"]
    ]);
    // Having yielded to the incoming offer, it must still answer normally.
    expect(peer.createAnswer).toHaveBeenCalled();
  });
  test("does not roll back when there is no local offer in flight", async () => {
    const mesh = makeMesh();
    const peer = mesh.createPeer("guest");
    peer.signalingState = "stable";
    await mesh.accept("guest", { description: { type: "offer", sdp: "their-offer" } });
    expect(peer.setLocalDescription).not.toHaveBeenCalledWith({ type: "rollback" });
  });
  test("validates each incoming start boundary independently", () => {
    const cases = [
      { transferId: 1, size: 0 },
      { transferId: "", size: 0 },
      { transferId: "t".repeat(129), size: 0 },
      { transferId: "t", size: -1 },
      { transferId: "t", size: 0.5 },
      { transferId: "t", size: Number.MAX_SAFE_INTEGER + 1 },
      { transferId: "t", size: 512 * 1024 * 1024 + 1 }
    ];
    for (const message of cases) {
      const { mesh, channel } = setupChannel("host");
      channel.onmessage({ data: JSON.stringify({ type: "file-start", ...message }) });
      expect(mesh.transfers.incomingFiles.size).toBe(0);
    }
    for (const size of [0, 64 * 1024 * 1024]) {
      const { mesh, channel } = setupChannel("host");
      channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size }) });
      expect(mesh.transfers.incomingFiles.get("host").metadata.size).toBe(size);
    }
  });
  test("matches confirmations by transfer and participant with exact errors", () => {
    const mesh = makeMesh();
    const host = new FakeChannel();
    const guest = new FakeChannel();
    mesh.setupDataChannel("host", host);
    mesh.setupDataChannel("guest", guest);
    const resolve = vi.fn();
    const reject = vi.fn();
    const pending = { participantId: "host", channel: host, resolve, reject, timer: 10 };
    mesh.transfers.pendingTransferConfirmations.set("t", pending);
    guest.onmessage({ data: JSON.stringify({ type: "file-complete", transferId: "t" }) });
    host.onmessage({ data: JSON.stringify({ type: "file-complete", transferId: "wrong" }) });
    expect(mesh.transfers.pendingTransferConfirmations.get("t")).toBe(pending);
    expect(resolve).not.toHaveBeenCalled();
    host.onmessage({ data: JSON.stringify({ type: "file-complete", transferId: "t" }) });
    expect(resolve).toHaveBeenCalledOnce();
    expect(reject).not.toHaveBeenCalled();
    expect(mesh.transfers.pendingTransferConfirmations.has("t")).toBe(false);
    mesh.transfers.pendingTransferConfirmations.set("error", {
      participantId: "host",
      channel: host,
      resolve,
      reject,
      timer: 11
    });
    host.onmessage({
      data: JSON.stringify({ type: "file-error", transferId: "error", error: "e".repeat(501) })
    });
    expect(reject).toHaveBeenLastCalledWith(expect.objectContaining({ message: "e".repeat(500) }));
    mesh.transfers.pendingTransferConfirmations.set("fallback", {
      participantId: "host",
      channel: host,
      resolve,
      reject,
      timer: 12
    });
    host.onmessage({
      data: JSON.stringify({ type: "file-error", transferId: "fallback", error: 1 })
    });
    expect(reject).toHaveBeenLastCalledWith(expect.objectContaining({ message: "Получатель не смог принять песню" }));
  });
  test.each(["missing", "id", "size"])("rejects an incomplete file when only %s is invalid", (invalid) => {
    const { mesh, channel } = setupChannel("host");
    if (invalid !== "missing") {
      channel.onmessage({
        data: JSON.stringify({ type: "file-start", transferId: "t", size: 1 })
      });
      if (invalid !== "size") channel.onmessage({ data: new Uint8Array([1]) });
    }
    channel.onmessage({
      data: JSON.stringify({ type: "file-end", transferId: invalid === "id" ? "wrong" : "t" })
    });
    expect(JSON.parse(channel.send.mock.calls.at(-1)[0])).toMatchObject({
      type: "file-error",
      transferId: invalid === "id" ? "wrong" : "t",
      error: invalid === "id" ? "Получен неизвестный идентификатор передачи" : "Получен неполный файл песни"
    });
  });
  test("ignores unrelated binary data and duplicate active starts", async () => {
    const { mesh, channel } = setupChannel("host");
    for (const data of [new Uint8Array([1]), {}, new Uint8Array([])]) {
      channel.onmessage({ data });
      expect(mesh.transfers.incomingFiles.size).toBe(0);
    }
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "one", size: 2 }) });
    const original = mesh.transfers.incomingFiles.get("host");
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "two", size: 2 }) });
    expect(mesh.transfers.incomingFiles.get("host")).toBe(original);
    channel.onmessage({ data: new Uint8Array([]) });
    expect(original.received).toBe(0);
    channel.onmessage({ data: new Uint8Array([1]) });
    expect(original.received).toBe(1);
    await vi.waitFor(() => expect(original.chunks).toHaveLength(1));
  });
  test("passes the exact wait contract and accepts the backpressure boundary", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    channel.bufferedAmount = 512 * 1024;
    mesh.setupDataChannel("guest", channel);
    mesh.transfers.waitForDataChannel = vi.fn().mockResolvedValue(channel);
    const sending = mesh.sendFile("guest", new Blob(["x"]), {
      kind: 1,
      songId: null,
      filename: {},
      ignored: true
    });
    await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
    expect(mesh.transfers.waitForDataChannel).toHaveBeenCalledWith("guest", 15_000, 0, expect.anything());
    const transferId = [...mesh.transfers.pendingTransferConfirmations.keys()][0];
    expect(JSON.parse(channel.send.mock.calls[0][0])).toEqual({
      type: "file-start",
      transferId,
      size: 1,
      mimeType: "application/octet-stream"
    });
    channel.send.mockClear();
    mesh.setupDataChannel("guest", channel);
    channel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId }) });
    await sending;
  });
  test.each(["before-read", "after-read", "after-chunk"])("cancels a transfer from lifecycle invalidation %s", async (phase) => {
    const { mesh, channel } = setupChannel();
    if (phase === "before-read") {
      const defaultSend = channel.send.getMockImplementation();
      channel.send.mockImplementationOnce((value) => {
        mesh.lifecycleVersion += 1;
        return defaultSend(value);
      });
    }
    class InvalidatingBlob extends Blob {
      slice(...args) {
        const sliced = super.slice(...args);
        return {
          arrayBuffer: async () => {
            if (phase === "after-read") mesh.lifecycleVersion += 1;
            return sliced.arrayBuffer();
          }
        };
      }
    }
    if (phase === "after-chunk") {
      const defaultSend = channel.send.getMockImplementation();
      channel.send.mockImplementation((value) => {
        const result = defaultSend(value);
        if (value instanceof ArrayBuffer) mesh.lifecycleVersion += 1;
        return result;
      });
    }
    await expect(mesh.sendFile("guest", new InvalidatingBlob(["x"]))).rejects.toThrow("Передача файла отменена");
  });
  test("clears only owned timers and reports peer removal exactly", () => {
    const clear = vi.spyOn(globalThis, "clearTimeout");
    const mesh = makeMesh();
    mesh.onPeerClosed = vi.fn();
    mesh.disconnectTimers.set("guest", 21);
    mesh.transfers.incomingFiles.set("guest", { timer: 22 });
    mesh.transfers.channels.set("guest", new FakeChannel());
    const reject = vi.fn();
    mesh.transfers.pendingTransferConfirmations.set("owned", {
      participantId: "guest",
      timer: 23,
      reject,
      resolve: vi.fn()
    });
    mesh.removePeer("guest");
    expect(clear).toHaveBeenCalledWith(21);
    expect(clear).toHaveBeenCalledWith(22);
    expect(clear).toHaveBeenCalledWith(23);
    expect(reject).toHaveBeenCalledWith(
      expect.objectContaining({
        message: "Участник отключился во время передачи. Отправьте файл заново — продолжить прерванную передачу нельзя."
      })
    );
    expect(mesh.onPeerClosed).toHaveBeenCalledWith("guest");
    clear.mockClear();
    mesh.transfers.incomingFiles.set("untimed", { timer: 0 });
    mesh.removePeer("untimed");
    expect(clear).not.toHaveBeenCalled();
    expect(mesh.onPeerClosed).toHaveBeenCalledTimes(1);
  });
  test("handles exact binary, live-track and duplicate-track semantics", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    let blob;
    mesh.onFile = vi.fn((_id, value) => {
      blob = value;
      return true;
    });
    mesh.setupDataChannel("host", channel);
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "buffer", size: 2 })
    });
    const binary = new Uint8Array([7, 8]).buffer;
    channel.onmessage({ data: binary });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "buffer" }) });
    await vi.waitFor(() => expect(blob).toBeTruthy());
    expect([...new Uint8Array(await blob.arrayBuffer())]).toEqual([7, 8]);
    const ended = track("ended", "ended");
    const live = track("live", "live");
    const mixed = stream([ended, live]);
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia: vi.fn() } }
    });
    mesh.stream = mixed;
    expect(await mesh.start()).toBe(mixed);
    const peer = mesh.createPeer("guest");
    peer.addTrack.mockClear();
    peer.senders = [{}, { track: {} }, { track: track("existing") }];
    const existing = track("existing");
    const added = track("added");
    const media = stream([existing, added]);
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(media) } }
    });
    mesh.stream = null;
    await mesh.start();
    expect(peer.addTrack).toHaveBeenCalledTimes(1);
    expect(peer.addTrack).toHaveBeenCalledWith(added, media);
  });
  test("keeps foreign in-flight promise ownership intact", async () => {
    let releaseCapture;
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: {
        mediaDevices: {
          getUserMedia: vi.fn(
            () =>
              new Promise((resolve) => {
                releaseCapture = resolve;
              })
          )
        }
      }
    });
    const mesh = makeMesh();
    const starting = mesh.start();
    await vi.waitFor(() => expect(releaseCapture).toBeTypeOf("function"));
    const foreignStart = Promise.resolve("foreign");
    mesh.startPromise = foreignStart;
    releaseCapture(stream());
    await starting;
    expect(mesh.startPromise).toBe(foreignStart);
    const invite = mesh.invite("guest");
    const foreignInvite = Promise.resolve("foreign");
    mesh.invitePromises.set("guest", foreignInvite);
    await invite;
    expect(mesh.invitePromises.get("guest")).toBe(foreignInvite);
    let releasePrevious;
    mesh.signalPromises.set(
      "signal",
      new Promise((resolve) => {
        releasePrevious = resolve;
      })
    );
    const accepted = mesh.accept("signal", { candidate: "ice" });
    const foreignSignal = Promise.resolve("foreign");
    mesh.signalPromises.set("signal", foreignSignal);
    releasePrevious();
    await accepted;
    expect(mesh.signalPromises.get("signal")).toBe(foreignSignal);
  });
  test("tolerates missing optional event hooks on current and stale peers", () => {
    const mesh = makeMesh();
    const peer = mesh.createPeer("guest");
    expect(() => peer.ontrack({ streams: [] })).not.toThrow();
    expect(() => peer.ontrack({ streams: [stream()] })).not.toThrow();
    mesh.removePeer("guest");
    expect(() => peer.ontrack({ streams: [{ getTracks: undefined }] })).not.toThrow();
    expect(() => peer.ondatachannel({ channel: {} })).not.toThrow();
    expect(() => peer.onconnectionstatechange()).not.toThrow();
    mesh.stream = { getAudioTracks: undefined };
    expect(() => mesh.setMicrophoneMuted(true)).not.toThrow();
  });
  test("accepts the maximum participant id and default sender encodings", async () => {
    const mesh = makeMesh();
    expect(await mesh.accept("g".repeat(128), { candidate: "ice" })).toBe(true);
    const sender = { track: track(), getParameters: () => ({}), setParameters: vi.fn() };
    await mesh.optimizeAudioSenders({ getSenders: () => [sender] });
    expect(sender.setParameters).toHaveBeenCalledWith({
      encodings: [{ maxBitrate: 128_000, priority: "high", networkPriority: "high" }],
      degradationPreference: "maintain-framerate"
    });
  });
  test("accepts the maximum control-message size and rejects one byte more", () => {
    const makeStart = (length) => {
      const base = JSON.stringify({ type: "file-start", transferId: "t", size: 0, padding: "" });
      return JSON.stringify({
        type: "file-start",
        transferId: "t",
        size: 0,
        padding: "x".repeat(length - base.length)
      });
    };
    const acceptedMesh = makeMesh();
    const acceptedChannel = new FakeChannel();
    acceptedMesh.setupDataChannel("host", acceptedChannel);
    const maximum = makeStart(16 * 1024);
    expect(maximum).toHaveLength(16 * 1024);
    acceptedChannel.onmessage({ data: maximum });
    expect(acceptedMesh.transfers.incomingFiles.has("host")).toBe(true);
    const rejectedMesh = makeMesh();
    const rejectedChannel = new FakeChannel();
    rejectedMesh.setupDataChannel("host", rejectedChannel);
    rejectedChannel.onmessage({ data: `${maximum} ` });
    expect(rejectedMesh.transfers.incomingFiles.size).toBe(0);
  });
  test.each([null, [], 1, "text"])("rejects structurally invalid decoded control message %#", (message) => {
    const { mesh, channel } = setupChannel("host");
    channel.onmessage({ data: JSON.stringify(message) });
    expect(mesh.transfers.incomingFiles.size).toBe(0);
    expect(channel.send).not.toHaveBeenCalled();
  });
  test("emits the first one-percent receive update and keeps Blob type", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.onTransferProgress = vi.fn();
    let blob;
    mesh.onFile = vi.fn((_id, value) => {
      blob = value;
      return true;
    });
    mesh.setupDataChannel("host", channel);
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size: 100 }) });
    channel.onmessage({ data: new Uint8Array([1]) });
    expect(mesh.onTransferProgress).toHaveBeenLastCalledWith(expect.objectContaining({ stage: "receiving", percent: 1 }));
    channel.onmessage({ data: new Uint8Array(99) });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "t" }) });
    await vi.waitFor(() => expect(blob).toBeTruthy());
    expect(blob.type).toBe("application/octet-stream");
  });
  test("does not let stale channels clear a replacement channel", () => {
    const mesh = makeMesh();
    const first = new FakeChannel();
    const second = new FakeChannel();
    mesh.setupDataChannel("guest", first);
    mesh.setupDataChannel("guest", second);
    first.onclose();
    first.onerror();
    expect(mesh.transfers.channels.get("guest")).toBe(second);
    second.onclose();
    expect(mesh.transfers.channels.has("guest")).toBe(false);
  });
  test.each([
    { failure: new Error("broken"), sent: "broken" },
    { failure: "plain", sent: "plain" },
    {
      failure: () => {
        throw new Error("sync");
      },
      sent: "sync"
    }
  ])("reports exact import failure %#", async ({ failure, sent }) => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.onTransferProgress = vi.fn();
    mesh.onFile = typeof failure === "function" ? failure : vi.fn().mockRejectedValue(failure);
    mesh.setupDataChannel("host", channel);
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size: 1 }) });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "t" }) });
    await vi.waitFor(() =>
      expect(
        channel.send.mock.calls.some(([value]) => {
          if (typeof value !== "string") return false;
          const message = JSON.parse(value);
          return message.type === "file-error" && message.error === sent;
        })
      ).toBe(true)
    );
    expect(mesh.onTransferProgress).toHaveBeenLastCalledWith(
      expect.objectContaining({ participantId: "host", stage: "error", percent: 100 })
    );
  });
  test("completes an import and suppresses only the closed reply", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.onTransferProgress = vi.fn();
    mesh.onFile = vi.fn().mockResolvedValue(true);
    mesh.setupDataChannel("host", channel);
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size: 0 }) });
    channel.send.mockClear();
    channel.readyState = "closed";
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "t" }) });
    await vi.waitFor(() =>
      expect(mesh.onTransferProgress).toHaveBeenLastCalledWith(
        expect.objectContaining({ participantId: "host", stage: "complete", percent: 100 })
      )
    );
    expect(channel.send).not.toHaveBeenCalled();
  });
  test("normalizes polling timeouts and distinguishes closing channels", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    mesh.transfers.channels.set("closing", new FakeChannel("closing"));
    await expect(mesh.waitForDataChannel("closing")).rejects.toThrow("Канал передачи песни закрыт");
    const numeric = mesh.waitForDataChannel("numeric", "50");
    const numericResult = expect(numeric).rejects.toThrow("Канал передачи песни не готов");
    await vi.advanceTimersByTimeAsync(49);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(1);
    await numericResult;
    const clamped = mesh.waitForDataChannel("maximum", 60_001);
    const clampedResult = expect(clamped).rejects.toThrow("Канал передачи песни не готов");
    await vi.advanceTimersByTimeAsync(59_999);
    expect(vi.getTimerCount()).toBeGreaterThan(0);
    await vi.advanceTimersByTimeAsync(1);
    await clampedResult;
  });
  test("does not declare backpressure stalled at the exact timeout boundary", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const channel = new FakeChannel();
    channel.bufferedAmount = 512 * 1024 + 1;
    mesh.setupDataChannel("guest", channel);
    const sending = mesh.sendFile("guest", new Blob(["x"]));
    let settled = false;
    sending.then(
      () => {
        settled = true;
      },
      () => {
        settled = true;
      }
    );
    await vi.advanceTimersByTimeAsync(30_000);
    expect(settled).toBe(false);
    await vi.advanceTimersByTimeAsync(20);
    await expect(sending).rejects.toThrow("Передача песни остановилась: нет ответа от участника");
  });
  test("uses the exact receiver-confirmation timeout", async () => {
    vi.useFakeTimers();
    const { mesh, channel } = setupChannel();
    const sending = mesh.sendFile("guest", new Blob([]));
    const result = expect(sending).rejects.toThrow("Участник не подтвердил получение песни");
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(5 * 60_000 - 1);
    expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1);
    await vi.advanceTimersByTimeAsync(1);
    await result;
    expect(mesh.transfers.pendingTransferConfirmations.size).toBe(0);
  });
  test("keeps invalid binary and absent callbacks side-effect free", () => {
    const { mesh, channel } = setupChannel("host");
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size: 1 }) });
    const transfer = mesh.transfers.incomingFiles.get("host");
    channel.onmessage({ data: {} });
    expect(transfer).toMatchObject({ received: 0, chunks: [] });
    const peer = mesh.createPeer("guest");
    mesh.onRemoteStream = vi.fn();
    peer.ontrack({ streams: [] });
    expect(mesh.onRemoteStream).not.toHaveBeenCalled();
    mesh.stream = null;
    expect(() => mesh.setMicrophoneMuted(true)).not.toThrow();
  });
  test("does not process stale connection or queued-signal work", async () => {
    const clear = vi.spyOn(globalThis, "clearTimeout");
    const mesh = makeMesh();
    const peer = mesh.createPeer("stale");
    mesh.peers.delete("stale");
    const version = mesh.peerVersions.get("stale");
    peer.connectionState = "failed";
    peer.onconnectionstatechange();
    expect(mesh.peerVersions.get("stale")).toBe(version);
    const current = mesh.createPeer("current");
    const connectTimer = mesh.connectTimers.get("current");
    current.connectionState = "connected";
    current.onconnectionstatechange();
    expect(clear).toHaveBeenCalledWith(connectTimer);
    expect(mesh.connectTimers.has("current")).toBe(false);
    let release;
    mesh.signalPromises.set(
      "queued",
      new Promise((resolve) => {
        release = resolve;
      })
    );
    const queued = mesh.accept("queued", { candidate: "ice" });
    mesh.peerVersions.set("queued", 1);
    release();
    await expect(queued).resolves.toBe(false);
    expect(mesh.peers.has("queued")).toBe(false);
  });
  test("rejects arrays before peer creation and has no phantom ICE replay", async () => {
    const mesh = makeMesh();
    expect(await mesh.accept("guest", [])).toBe(false);
    expect(mesh.peers.has("guest")).toBe(false);
    expect(await mesh.accept("answer", { description: { type: "answer", sdp: "answer" } })).toBe(true);
    expect(FakePeer.instances.at(-1).addIceCandidate).not.toHaveBeenCalled();
  });
  test("does not set a local answer after answer creation becomes stale", async () => {
    let release;
    globalThis.RTCPeerConnection = class DeferredAnswerPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.createAnswer = vi.fn(
          () =>
            new Promise((resolve) => {
              release = resolve;
            })
        );
      }
    };
    const mesh = makeMesh();
    const accepted = mesh.accept("guest", { description: { type: "offer", sdp: "offer" } });
    await vi.waitFor(() => expect(release).toBeTypeOf("function"));
    const peer = FakePeer.instances.at(-1);
    mesh.lifecycleVersion += 1;
    release({ type: "answer", sdp: "answer" });
    await expect(accepted).resolves.toBe(false);
    expect(peer.setLocalDescription).not.toHaveBeenCalled();
  });
  test("tolerates optional channel methods and timer owners", () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    mesh.transfers.channels.set("guest", { readyState: "open" });
    expect(() => mesh.setupDataChannel("guest", new FakeChannel())).not.toThrow();
    const timer = mesh.transfers.createIncomingTransferTimer("missing", "t");
    expect(() => vi.advanceTimersByTime(30_000)).not.toThrow();
    clearTimeout(timer);
    mesh.transfers.incomingFiles.set("orphan", { metadata: { transferId: "t" }, timer: 0 });
    const orphan = mesh.transfers.createIncomingTransferTimer("orphan", "t");
    expect(() => vi.advanceTimersByTime(30_000)).not.toThrow();
    clearTimeout(orphan);
  });
  test("uses exact generated transfer ids and accepts the maximum file size", async () => {
    const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", {
      configurable: true,
      value: { randomUUID: () => "exact-uuid" }
    });
    const { mesh, channel } = setupChannel();
    const sending = mesh.sendFile("guest", new Blob([]), null);
    await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
    expect(JSON.parse(channel.send.mock.calls[0][0]).transferId).toBe("exact-uuid");
    channel.onmessage({
      data: JSON.stringify({ type: "file-complete", transferId: "exact-uuid" })
    });
    await sending;
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: {} });
    const date = vi.spyOn(Date, "now").mockReturnValue(1_000);
    const random = vi.spyOn(Math, "random").mockReturnValue(0.5);
    const fallbackChannel = new FakeChannel();
    const fallbackMesh = makeMesh();
    fallbackMesh.setupDataChannel("fallback", fallbackChannel);
    const fallback = fallbackMesh.sendFile("fallback", new Blob([]));
    await vi.waitFor(() => expect(fallbackMesh.transfers.pendingTransferConfirmations.size).toBe(1));
    expect(JSON.parse(fallbackChannel.send.mock.calls[0][0]).transferId).toBe("1000-8");
    fallbackChannel.onmessage({
      data: JSON.stringify({ type: "file-complete", transferId: "1000-8" })
    });
    await fallback;
    date.mockRestore();
    random.mockRestore();
    if (cryptoDescriptor) Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
    const maximum = Object.create(Blob.prototype);
    Object.defineProperties(maximum, { size: { value: 512 * 1024 * 1024 }, type: { value: "" } });
    const closed = new FakeChannel("closed");
    const maximumMesh = makeMesh();
    maximumMesh.transfers.waitForDataChannel = vi.fn().mockResolvedValue(closed);
    await expect(maximumMesh.sendFile("guest", maximum)).rejects.toThrow("Канал передачи песни закрыт");
  });
  test("emits receive progress only when its integer percent changes", () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.onTransferProgress = vi.fn();
    mesh.setupDataChannel("host", channel);
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "t", size: 1_000 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    const afterFirst = mesh.onTransferProgress.mock.calls.length;
    channel.onmessage({ data: new Uint8Array([1]) });
    expect(mesh.onTransferProgress).toHaveBeenCalledTimes(afterFirst);
    channel.onmessage({ data: new Uint8Array(8) });
    expect(mesh.onTransferProgress).toHaveBeenCalledTimes(afterFirst + 1);
    expect(mesh.onTransferProgress).toHaveBeenLastCalledWith(expect.objectContaining({ percent: 1 }));
  });
  test("suppresses an import-error reply only after its channel closes", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.onFile = vi.fn().mockRejectedValue(new Error("broken"));
    mesh.onTransferProgress = vi.fn();
    mesh.setupDataChannel("host", channel);
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size: 0 }) });
    const sendsBeforeClose = channel.send.mock.calls.length;
    channel.readyState = "closed";
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "t" }) });
    await vi.waitFor(() =>
      expect(mesh.onTransferProgress).toHaveBeenLastCalledWith(expect.objectContaining({ stage: "error", percent: 100 }))
    );
    expect(channel.send).toHaveBeenCalledTimes(sendsBeforeClose);
  });
  test("does not clear absent transfer timers while stopping", () => {
    const mesh = makeMesh();
    const clear = vi.spyOn(globalThis, "clearTimeout");
    mesh.transfers.incomingFiles.set("untimed", { timer: 0 });
    mesh.stop();
    expect(clear).not.toHaveBeenCalled();
  });
  test("rejects non-object signals before creating a peer", async () => {
    const mesh = makeMesh();
    expect(await mesh.accept("guest", "signal")).toBe(false);
    expect(mesh.peers.size).toBe(0);
  });
  test("does not replace a data channel with itself", () => {
    const { mesh, channel } = setupChannel();
    mesh.setupDataChannel("guest", channel);
    expect(channel.close).not.toHaveBeenCalled();
    expect(mesh.transfers.channels.get("guest")).toBe(channel);
  });
  test("does not clear or reply for a missing incomplete transfer on a closed channel", () => {
    const clear = vi.spyOn(globalThis, "clearTimeout");
    const mesh = makeMesh();
    const channel = new FakeChannel("closed");
    mesh.setupDataChannel("host", channel);
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "missing" }) });
    expect(clear).not.toHaveBeenCalled();
    expect(channel.send).not.toHaveBeenCalled();
  });
  test("validates the Blob runtime and truncates outbound MIME types", async () => {
    const BlobClass = globalThis.Blob;
    globalThis.Blob = undefined;
    await expect(makeMesh().sendFile("guest", {})).rejects.toThrow("Для передачи нужны участник и файл");
    globalThis.Blob = BlobClass;
    const { mesh, channel } = setupChannel();
    const payload = new Blob([], { type: `x/${"a".repeat(300)}` });
    const sending = mesh.sendFile("guest", payload);
    await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
    const start = JSON.parse(channel.send.mock.calls[0][0]);
    expect(start.mimeType).toHaveLength(255);
    expect(channel.send.mock.calls.filter(([value]) => value instanceof ArrayBuffer)).toHaveLength(0);
    channel.onmessage({
      data: JSON.stringify({ type: "file-complete", transferId: start.transferId })
    });
    await sending;
  });
  test("generates a fallback id when the crypto object is absent", async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: undefined });
    const { mesh, channel } = setupChannel();
    const sending = mesh.sendFile("guest", new Blob([]));
    await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
    const transferId = [...mesh.transfers.pendingTransferConfirmations.keys()][0];
    expect(transferId).toMatch(/^\d+-[\da-f]+$/u);
    channel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId }) });
    await sending;
    if (descriptor) Object.defineProperty(globalThis, "crypto", descriptor);
  });
  test.each(["lifecycle", "channel"])("stops before reading a chunk when %s becomes invalid", async (invariant) => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    const initialVersion = mesh.lifecycleVersion;
    channel.send.mockImplementation((value) => {
      if (typeof value !== "string") return;
      const message = JSON.parse(value);
      if (message.type === "file-start") {
        if (invariant === "lifecycle") mesh.lifecycleVersion += 1;
        else channel.readyState = "closed";
      } else if (message.type === "file-end") {
        queueMicrotask(() =>
          channel.onmessage({
            data: JSON.stringify({ type: "file-complete", transferId: message.transferId })
          })
        );
      }
    });
    mesh.setupDataChannel("guest", channel);
    class RestoringBlob extends Blob {
      slice(...args) {
        const sliced = super.slice(...args);
        return {
          arrayBuffer: async () => {
            mesh.lifecycleVersion = initialVersion;
            channel.readyState = "open";
            return sliced.arrayBuffer();
          }
        };
      }
    }
    await expect(mesh.sendFile("guest", new RestoringBlob(["x"]))).rejects.toThrow("Передача файла отменена");
    expect(channel.send.mock.calls.some(([value]) => value instanceof ArrayBuffer)).toBe(false);
  });
  test("ignores unknown control types and clears a completed transfer timer", () => {
    vi.useFakeTimers();
    const { mesh, channel } = setupChannel("host");
    channel.onmessage({ data: JSON.stringify({ type: "unknown" }) });
    expect(() => channel.onmessage({ data: JSON.stringify({ type: "__proto__" }) })).not.toThrow();
    expect(channel.send).not.toHaveBeenCalled();
    expect(mesh.transfers.incomingFiles.size).toBe(0);
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "t", size: 0 }) });
    const { timer } = mesh.transfers.incomingFiles.get("host");
    const clear = vi.spyOn(globalThis, "clearTimeout");
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "t" }) });
    expect(clear).toHaveBeenCalledWith(timer);
  });
  test.each(["lifecycle", "channel"])("stops after reading a chunk when %s becomes invalid", async (invariant) => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    const initialVersion = mesh.lifecycleVersion;
    const defaultSend = channel.send.getMockImplementation();
    channel.send.mockImplementation((value) => {
      if (value instanceof ArrayBuffer) {
        mesh.lifecycleVersion = initialVersion;
        channel.readyState = "open";
        return defaultSend(value);
      }
      return defaultSend(value);
    });
    mesh.setupDataChannel("guest", channel);
    class InvalidatingBlob extends Blob {
      slice(...args) {
        const sliced = super.slice(...args);
        return {
          arrayBuffer: async () => {
            const result = await sliced.arrayBuffer();
            if (invariant === "lifecycle") mesh.lifecycleVersion += 1;
            else channel.readyState = "closed";
            return result;
          }
        };
      }
    }
    await expect(mesh.sendFile("guest", new InvalidatingBlob(["x"]))).rejects.toThrow("Передача файла отменена");
    expect(channel.send.mock.calls.some(([value]) => value instanceof ArrayBuffer)).toBe(false);
  });
  test.each(["lifecycle", "channel"])("stops inside backpressure when %s becomes invalid", async (invariant) => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    const initialVersion = mesh.lifecycleVersion;
    channel.bufferedAmount = 512 * 1024 + 1;
    channel.send.mockImplementation((value) => {
      if (typeof value !== "string") return;
      const message = JSON.parse(value);
      if (message.type === "file-start") {
        if (invariant === "lifecycle") mesh.lifecycleVersion += 1;
        else channel.readyState = "closed";
        setTimeout(() => {
          mesh.lifecycleVersion = initialVersion;
          channel.readyState = "open";
          channel.bufferedAmount = 0;
        }, 0);
      } else if (message.type === "file-end") {
        queueMicrotask(() =>
          channel.onmessage({
            data: JSON.stringify({ type: "file-complete", transferId: message.transferId })
          })
        );
      }
    });
    mesh.setupDataChannel("guest", channel);
    await expect(mesh.sendFile("guest", new Blob(["x"]))).rejects.toThrow("Передача файла отменена");
  });
  test("starts, reuses and restarts microphone streams safely", async () => {
    const mesh = makeMesh();
    await expect(mesh.start()).rejects.toThrow("Захват микрофона не поддерживается в этом окружении");
    const firstTrack = track("first");
    const firstStream = stream([firstTrack]);
    const secondTrack = track("second");
    const secondStream = stream([secondTrack]);
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: {
        mediaDevices: {
          getUserMedia: vi.fn().mockResolvedValueOnce(firstStream).mockResolvedValueOnce(secondStream)
        }
      }
    });
    await expect(mesh.start()).resolves.toBe(firstStream);
    expect(firstTrack.contentHint).toBe("music");
    await expect(mesh.start()).resolves.toBe(firstStream);
    firstTrack.readyState = "ended";
    await expect(mesh.start()).resolves.toBe(secondStream);
    expect(firstTrack.stop).toHaveBeenCalled();
  });
  test("deduplicates concurrent microphone startup and cancels stale capture", async () => {
    let resolveCapture;
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: {
        mediaDevices: {
          getUserMedia: vi.fn(
            () =>
              new Promise((resolve) => {
                resolveCapture = resolve;
              })
          )
        }
      }
    });
    const mesh = makeMesh();
    const media = stream();
    const first = mesh.start();
    const second = mesh.start();
    await vi.waitFor(() => expect(navigator.mediaDevices.getUserMedia).toHaveBeenCalledTimes(1));
    mesh.stop();
    resolveCapture(media);
    await expect(first).rejects.toThrow("Запуск микрофона отменён");
    await expect(second).rejects.toThrow("Запуск микрофона отменён");
    expect(media.getTracks()[0].stop).toHaveBeenCalled();
  });
  test("creates one peer, routes events and removes failed connections", () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const remote = vi.fn();
    const closed = vi.fn();
    mesh.onRemoteStream = remote;
    mesh.onPeerClosed = closed;
    mesh.stream = stream([track("local")]);
    expect(() => mesh.createPeer("")).toThrow(TypeError);
    delete globalThis.RTCPeerConnection;
    expect(() => mesh.createPeer("guest")).toThrow(Error);
    globalThis.RTCPeerConnection = FakePeer;
    const peer = mesh.createPeer("guest");
    expect(mesh.createPeer("guest")).toBe(peer);
    expect(peer.configuration.iceServers[0].urls).toContain("cloudflare");
    expect(peer.addTrack).toHaveBeenCalled();
    peer.onicecandidate({ candidate: { candidate: "ice" } });
    expect(mesh.roomClient.send).toHaveBeenCalledWith("signal", {
      targetId: "guest",
      signal: { candidate: { candidate: "ice" } }
    });
    peer.onicecandidate({ candidate: null });
    const remoteStream = stream([track("remote")]);
    peer.ontrack({ streams: [remoteStream] });
    peer.ontrack({ streams: [] });
    expect(remote).toHaveBeenCalledWith("guest", remoteStream);
    const channel = new FakeChannel();
    peer.ondatachannel({ channel });
    expect(mesh.transfers.channels.get("guest")).toBe(channel);
    peer.connectionState = "disconnected";
    peer.onconnectionstatechange();
    peer.connectionState = "connected";
    peer.onconnectionstatechange();
    vi.advanceTimersByTime(10_000);
    expect(mesh.peers.has("guest")).toBe(true);
    peer.connectionState = "failed";
    peer.onconnectionstatechange();
    vi.advanceTimersByTime(15_000);
    expect(mesh.peers.has("guest")).toBe(false);
    expect(closed).toHaveBeenCalledWith("guest");
    const transient = mesh.createPeer("transient");
    transient.connectionState = "disconnected";
    transient.onconnectionstatechange();
    transient.connectionState = "connected";
    vi.advanceTimersByTime(10_000);
    expect(mesh.peers.has("transient")).toBe(true);
  });
  test("optimizes audio senders without rejecting unsupported senders", async () => {
    const mesh = makeMesh();
    const configured = {
      track: track(),
      getParameters: () => ({ encodings: [] }),
      setParameters: vi.fn().mockResolvedValue(undefined)
    };
    const rejected = {
      track: track("other"),
      getParameters: () => ({ encodings: [{}] }),
      setParameters: vi.fn().mockRejectedValue(new Error("unsupported"))
    };
    await expect(mesh.optimizeAudioSenders({ getSenders: () => [configured, rejected, {}] })).resolves.toBeUndefined();
    expect(configured.setParameters.mock.calls[0][0].encodings[0]).toMatchObject({
      maxBitrate: 128_000,
      priority: "high",
      networkPriority: "high"
    });
  });
  test("invites once and sends the current local description", async () => {
    const mesh = makeMesh();
    expect(await mesh.invite("")).toBe(false);
    const first = mesh.invite("guest");
    const second = mesh.invite("guest");
    await expect(first).resolves.toBe(true);
    await expect(second).resolves.toBe(true);
    const peer = FakePeer.instances[0];
    expect(peer.createOffer).toHaveBeenCalledTimes(1);
    expect(peer.createDataChannel).toHaveBeenCalled();
    expect(mesh.roomClient.send).toHaveBeenCalledWith("signal", {
      targetId: "guest",
      signal: { description: peer.localDescription }
    });
    expect(mesh.invitePromises.size).toBe(0);
    const existingChannelMesh = makeMesh();
    existingChannelMesh.transfers.channels.set("guest", new FakeChannel());
    await expect(existingChannelMesh.invite("guest")).resolves.toBe(true);
    expect(FakePeer.instances.at(-1).createDataChannel).not.toHaveBeenCalled();
  });
  test("queues ICE candidates and answers offers in arrival order", async () => {
    const mesh = makeMesh();
    expect(await mesh.accept("", {})).toBe(false);
    expect(await mesh.accept("guest", [])).toBe(false);
    expect(await mesh.accept("guest", { candidate: "one" })).toBe(true);
    expect(await mesh.accept("guest", { candidate: "two" })).toBe(true);
    expect(await mesh.accept("guest", { description: { type: "offer", sdp: "x" } })).toBe(true);
    const peer = FakePeer.instances[0];
    expect(peer.addIceCandidate.mock.calls.map(([value]) => value)).toEqual(["one", "two"]);
    expect(peer.createAnswer).toHaveBeenCalled();
    expect(mesh.roomClient.send).toHaveBeenLastCalledWith("signal", {
      targetId: "guest",
      signal: { description: peer.localDescription }
    });
    expect(await mesh.accept("guest", { description: { type: "answer" } })).toBe(true);
    expect(await mesh.accept("guest", {})).toBe(false);
  });
  test("receives files, reports import progress and confirms completion", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    const progress = vi.fn();
    const received = vi.fn().mockResolvedValue(true);
    mesh.onTransferProgress = progress;
    mesh.onFile = received;
    mesh.setupDataChannel("host", channel);
    channel.onmessage({
      data: JSON.stringify({
        type: "file-start",
        transferId: "transfer",
        size: 3,
        filename: "song.zip",
        mimeType: "application/zip"
      })
    });
    channel.onmessage({ data: new Uint8Array([1, 2]) });
    channel.onmessage({ data: new Uint8Array([3]).buffer });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "transfer" }) });
    await vi.waitFor(() => expect(received).toHaveBeenCalled());
    await vi.waitFor(() => expect(progress.mock.calls.some(([event]) => event.stage === "complete")).toBe(true));
    expect(progress.mock.calls.map(([event]) => event.stage)).toEqual(expect.arrayContaining(["receiving", "importing", "complete"]));
    expect(channel.send).toHaveBeenCalledWith(JSON.stringify({ type: "file-complete", transferId: "transfer" }));
  });
  test("rejects malformed, incomplete and failed incoming transfers", async () => {
    const { mesh, channel } = setupChannel("host");
    channel.onmessage({ data: "bad" });
    channel.onmessage({ data: "[]" });
    channel.onmessage({ data: JSON.stringify({ type: "unknown" }) });
    channel.onmessage({ data: "x".repeat(20_000) });
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "", size: -1 }) });
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: 1, size: 1 }) });
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "short", size: 2 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "short" }) });
    expect(
      channel.send.mock.calls.some(
        ([value]) => typeof value === "string" && JSON.parse(value).type === "file-error" && JSON.parse(value).transferId === "short"
      )
    ).toBe(true);
    mesh.onFile = vi.fn().mockRejectedValue(new Error("import failed"));
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "failed", size: 1 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "failed" }) });
    await vi.waitFor(() =>
      expect(
        channel.send.mock.calls.some(([value]) => {
          if (typeof value !== "string") return false;
          const message = JSON.parse(value);
          return message.type === "file-error" && message.transferId === "failed";
        })
      ).toBe(true)
    );
    mesh.onFile = vi.fn(() => {
      throw new Error("synchronous import failure");
    });
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "sync-failed", size: 1 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    expect(() => channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "sync-failed" }) })).not.toThrow();
    await vi.waitFor(() =>
      expect(
        channel.send.mock.calls
          .map(([value]) => (typeof value === "string" ? JSON.parse(value) : null))
          .find((message) => message?.type === "file-error" && message.transferId === "sync-failed")
      ).toMatchObject({
        type: "file-error",
        transferId: "sync-failed",
        error: "synchronous import failure"
      })
    );
    channel.readyState = "closed";
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "missing" }) });
  });
  test("normalizes transfer metadata and suppresses replies to closed channels", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    const progress = vi.fn();
    mesh.onTransferProgress = progress;
    mesh.onFile = vi.fn().mockResolvedValue(true);
    mesh.setupDataChannel("host", channel);
    channel.onmessage({
      data: JSON.stringify({
        type: "file-start",
        transferId: "metadata",
        size: 1,
        kind: "song-package",
        songId: "song",
        filename: "song.zip",
        mimeType: "application/zip"
      })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.readyState = "closed";
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "metadata" }) });
    await Promise.resolve();
    await Promise.resolve();
    expect(progress).toHaveBeenCalled();
    channel.readyState = "open";
    mesh.onFile = vi.fn().mockRejectedValue("plain failure");
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "failed", size: 1 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.readyState = "closed";
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "failed" }) });
    await Promise.resolve();
    await Promise.resolve();
    channel.readyState = "open";
    mesh.onFile = vi.fn().mockRejectedValue("plain failure");
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "open-fail", size: 1 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "open-fail" }) });
    await Promise.resolve();
    await Promise.resolve();
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "same-percent", size: 1000 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.onmessage({ data: new Uint8Array([1]) });
  });
  test("rejects transfer admission before accepting chunks and never acknowledges a skipped import", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.canAcceptFile = vi.fn(() => false);
    mesh.setupDataChannel("guest", channel);
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "denied", size: 3 })
    });
    expect(mesh.transfers.incomingFiles.has("guest")).toBe(false);
    expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"type":"file-error"'));
    channel.onmessage({ data: new Uint8Array([1, 2, 3]) });
    expect(mesh.transfers.incomingFiles.has("guest")).toBe(false);
    mesh.canAcceptFile = vi.fn(() => true);
    mesh.onFile = vi.fn().mockResolvedValue(undefined);
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "skipped", size: 1 })
    });
    channel.onmessage({ data: new Uint8Array([1]) });
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "skipped" }) });
    await vi.waitFor(() =>
      expect(
        channel.send.mock.calls
          .map(([value]) => (typeof value === "string" ? JSON.parse(value) : null))
          .some((value) => value?.type === "file-error" && value.transferId === "skipped")
      ).toBe(true)
    );
    expect(
      channel.send.mock.calls
        .map(([value]) => (typeof value === "string" ? JSON.parse(value) : null))
        .some((value) => value?.type === "file-complete" && value.transferId === "skipped")
    ).toBe(false);
  });
  test("rejects a large no-OPFS transfer at file-start", () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    mesh.canAcceptFile = vi.fn(() => true);
    mesh.setupDataChannel("host", channel);
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "large", size: 65 * 1024 * 1024 })
    });
    expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
    expect(channel.send.mock.calls.map(([value]) => (typeof value === "string" ? JSON.parse(value) : null))).toContainEqual(
      expect.objectContaining({ type: "file-error", transferId: "large" })
    );
  });
  test("sends a file and waits for the matching receiver confirmation", async () => {
    const { mesh, channel } = setupChannel();
    const progress = vi.fn();
    mesh.onTransferProgress = progress;
    const sending = mesh.sendFile("guest", new Blob([new Uint8Array(40_000)], { type: "application/zip" }), {
      kind: "song-package",
      songId: "song",
      filename: "song.zip"
    });
    await vi.waitFor(() => {
      expect(channel.send.mock.calls.some(([value]) => typeof value === "string" && JSON.parse(value).type === "file-end")).toBe(true);
    });
    const end = channel.send.mock.calls
      .map(([value]) => (typeof value === "string" ? JSON.parse(value) : null))
      .find((value) => value?.type === "file-end");
    channel.onmessage({
      data: JSON.stringify({ type: "file-complete", transferId: end.transferId })
    });
    await expect(sending).resolves.toBeUndefined();
    expect(progress.mock.calls.at(-1)[0]).toMatchObject({
      participantId: "guest",
      stage: "complete",
      percent: 100
    });
    const cryptoDescriptor = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: {} });
    const fallback = mesh.sendFile("guest", new Blob(["x"]));
    await vi.waitFor(() => {
      expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1);
    });
    const fallbackId = [...mesh.transfers.pendingTransferConfirmations.keys()][0];
    channel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId: fallbackId }) });
    await expect(fallback).resolves.toBeUndefined();
    if (cryptoDescriptor) Object.defineProperty(globalThis, "crypto", cryptoDescriptor);
  });
  test("validates outbound files and cleans confirmation when final send throws", async () => {
    const mesh = makeMesh();
    await expect(mesh.sendFile("", new Blob([]))).rejects.toThrow(TypeError);
    const channel = new FakeChannel();
    let sends = 0;
    const defaultSend = channel.send.getMockImplementation();
    channel.send.mockImplementation((value) => {
      sends += 1;
      if (sends === 2) throw new Error("send failed");
      return defaultSend(value);
    });
    mesh.setupDataChannel("guest", channel);
    await expect(mesh.sendFile("guest", new Blob([]))).rejects.toThrow("send failed");
    expect(mesh.transfers.pendingTransferConfirmations.size).toBe(0);
  });
  test("closes replacement channels and clears all transfer resources on stop", () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const first = new FakeChannel();
    const second = new FakeChannel();
    mesh.setupDataChannel("guest", first);
    first.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "transfer", size: 10 })
    });
    const { timer } = mesh.transfers.incomingFiles.get("guest");
    const clearTimeoutSpy = vi.spyOn(globalThis, "clearTimeout");
    mesh.setupDataChannel("guest", second);
    expect(first.close).toHaveBeenCalled();
    expect(clearTimeoutSpy).toHaveBeenCalledWith(timer);
    mesh.transfers.incomingFiles.set("orphan", { timer: 99 });
    mesh.transfers.channels.set("orphan", new FakeChannel());
    mesh.stop();
    expect(mesh.transfers.channels.size).toBe(0);
    expect(mesh.transfers.incomingFiles.size).toBe(0);
    const closedMesh = makeMesh();
    const closedChannel = new FakeChannel("closed");
    closedMesh.transfers.channels.set("guest", closedChannel);
    closedMesh.transfers.incomingFiles.set("guest", { timer: 0 });
    closedMesh.setupDataChannel("guest", new FakeChannel());
    expect(closedChannel.close).not.toHaveBeenCalled();
    closedMesh.transfers.incomingFiles.set("untimed", { timer: 0 });
    closedMesh.stop();
  });
  test("attaches a new microphone to existing peers and flushes pending invites", async () => {
    const mesh = makeMesh();
    const peer = mesh.createPeer("guest");
    peer.senders.push({ track: track("existing") });
    const media = stream([track("existing"), track("new")]);
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia: vi.fn().mockResolvedValue(media) } }
    });
    mesh.pendingInvites.add("queued");
    await mesh.start();
    expect(peer.addTrack).toHaveBeenCalledTimes(1);
    expect(mesh.roomClient.send).toHaveBeenCalled();
    mesh.setMicrophoneMuted(true);
    expect(media.getAudioTracks().every(({ enabled }) => !enabled)).toBe(true);
    mesh.setMicrophoneMuted(false);
    expect(media.getAudioTracks().every(({ enabled }) => enabled)).toBe(true);
  });
  test("rejects stale peer events and expires a disconnected peer", () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const peer = mesh.createPeer("guest");
    mesh.removePeer("guest");
    const remoteTrack = track("remote");
    peer.ontrack({ streams: [stream([remoteTrack])] });
    expect(remoteTrack.stop).toHaveBeenCalled();
    const staleChannel = new FakeChannel();
    peer.ondatachannel({ channel: staleChannel });
    expect(staleChannel.close).toHaveBeenCalled();
    peer.connectionState = "disconnected";
    peer.onconnectionstatechange();
    const current = mesh.createPeer("current");
    current.connectionState = "disconnected";
    current.onconnectionstatechange();
    vi.advanceTimersByTime(25_000);
    expect(mesh.peers.has("current")).toBe(false);
  });
  test("rotates TURN credentials and ICE-restarts before dropping a degraded peer", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const rotated = [
      {
        urls: ["turn:turn.cloudflare.com:3478?transport=udp"],
        username: "rotated-user",
        credential: "rotated-password"
      }
    ];
    mesh.roomClient.getIceServers = vi.fn().mockResolvedValue(rotated);
    mesh.onPeerRecovering = vi.fn();
    mesh.onPeerRecovered = vi.fn();
    await mesh.invite("guest");
    const peer = mesh.peers.get("guest");
    mesh.roomClient.send.mockClear();
    peer.createOffer.mockClear();

    peer.connectionState = "failed";
    peer.onconnectionstatechange();
    await mesh.recoveryPromises.get("guest");

    expect(mesh.onPeerRecovering).toHaveBeenCalledWith("guest");
    expect(mesh.roomClient.getIceServers).toHaveBeenLastCalledWith({ force: true });
    expect(peer.setConfiguration).toHaveBeenLastCalledWith({
      iceServers: rotated,
      bundlePolicy: "max-bundle",
      rtcpMuxPolicy: "require",
      iceCandidatePoolSize: 4
    });
    expect(peer.createOffer).toHaveBeenLastCalledWith({ iceRestart: true });
    expect(mesh.roomClient.send).toHaveBeenCalledWith("signal", {
      targetId: "guest",
      signal: { description: peer.localDescription }
    });

    peer.connectionState = "connected";
    peer.onconnectionstatechange();
    expect(mesh.onPeerRecovered).toHaveBeenCalledWith("guest");
    vi.advanceTimersByTime(15_000);
    expect(mesh.peers.get("guest")).toBe(peer);
  });
  test("clears channels, rejects receiver errors and expires stalled imports", () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const channel = new FakeChannel();
    const reject = vi.fn();
    mesh.onTransferProgress = vi.fn();
    mesh.setupDataChannel("guest", channel);
    mesh.transfers.pendingTransferConfirmations.set("transfer", {
      participantId: "guest",
      channel,
      reject,
      resolve: vi.fn(),
      timer: 1
    });
    channel.onmessage({ data: JSON.stringify({ type: "file-error", transferId: "transfer" }) });
    expect(reject).toHaveBeenCalledWith(expect.any(Error));
    mesh.transfers.pendingTransferConfirmations.set("remote-error", {
      participantId: "guest",
      channel,
      reject,
      resolve: vi.fn(),
      timer: 2
    });
    channel.onmessage({
      data: JSON.stringify({
        type: "file-error",
        transferId: "remote-error",
        error: "remote import failed"
      })
    });
    expect(reject).toHaveBeenCalledWith(expect.objectContaining({ message: "remote import failed" }));
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "stalled", size: 10 })
    });
    vi.advanceTimersByTime(30_000);
    expect(JSON.parse(channel.send.mock.calls.at(-1)[0]).type).toBe("file-error");
    channel.onclose();
    expect(mesh.transfers.channels.has("guest")).toBe(false);
    channel.onerror();
  });
  test("waits for channels and reports closed, cancelled and timed out states", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    mesh.transfers.channels.set("open", new FakeChannel());
    await expect(mesh.waitForDataChannel("open")).resolves.toBe(mesh.transfers.channels.get("open"));
    mesh.transfers.channels.set("closed", new FakeChannel("closed"));
    await expect(mesh.waitForDataChannel("closed")).rejects.toThrow("Канал передачи песни закрыт");
    await expect(mesh.waitForDataChannel("missing", 0)).rejects.toThrow("Канал передачи песни не готов");
    const cancelled = mesh.waitForDataChannel("missing", 100, -1);
    await expect(cancelled).rejects.toThrow("Передача файла отменена");
    const timeout = mesh.waitForDataChannel("missing", 100);
    const timeoutResult = expect(timeout).rejects.toThrow("Канал передачи песни не готов");
    await vi.advanceTimersByTimeAsync(100);
    await timeoutResult;
    const invalidTimeout = mesh.waitForDataChannel("missing", "bad");
    const invalidResult = expect(invalidTimeout).rejects.toThrow("Канал передачи песни не готов");
    await vi.advanceTimersByTimeAsync(15_000);
    await invalidResult;
  });
  test("rejects oversized files and confirmation timeouts", async () => {
    class LargeBlob extends Blob {
      get size() {
        return 513 * 1024 * 1024;
      }
    }
    const mesh = makeMesh();
    await expect(mesh.sendFile("guest", new LargeBlob([]))).rejects.toThrow("Файл слишком большой для передачи через комнату");
    vi.useFakeTimers();
    const channel = new FakeChannel();
    mesh.setupDataChannel("guest", channel);
    const sending = mesh.sendFile("guest", new Blob([]));
    const result = expect(sending).rejects.toThrow("Участник не подтвердил получение песни");
    await Promise.resolve();
    await vi.advanceTimersByTimeAsync(5 * 60_000);
    await result;
  });
  test("recovers a failed signal queue and stops an active local stream", async () => {
    const mesh = makeMesh();
    mesh.signalPromises.set("guest", Promise.reject(new Error("old signal")));
    await expect(mesh.accept("guest", { candidate: "next" })).resolves.toBe(true);
    const localTrack = track("local");
    mesh.stream = stream([localTrack]);
    mesh.stop();
    expect(localTrack.stop).toHaveBeenCalledOnce();
  });
  test("cancels stale invites and direct ICE work", async () => {
    const mesh = makeMesh();
    let resolveOffer;
    globalThis.RTCPeerConnection = class DeferredPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.createOffer = vi.fn(
          () =>
            new Promise((resolve) => {
              resolveOffer = resolve;
            })
        );
      }
    };
    const invitation = mesh.invite("guest");
    mesh.removePeer("guest");
    resolveOffer({ type: "offer" });
    await expect(invitation).resolves.toBe(false);
    globalThis.RTCPeerConnection = FakePeer;
    const current = mesh.createPeer("current");
    current.remoteDescription = { type: "offer" };
    await expect(mesh.accept("current", { candidate: "ice" })).resolves.toBe(true);
    expect(current.addIceCandidate).toHaveBeenCalledWith("ice");
  });
  test("rejects excessive ICE candidates and invalid transfer messages", async () => {
    const mesh = makeMesh();
    mesh.pendingCandidates.set("guest", Array(256).fill("ice"));
    await expect(mesh.accept("guest", { candidate: "overflow" })).rejects.toThrow("Получено слишком много ICE-кандидатов");
    expect(mesh.peers.has("guest")).toBe(false);
    const channel = new FakeChannel();
    mesh.setupDataChannel("host", channel);
    channel.onmessage({ data: { unsupported: true } });
    channel.onmessage({ data: new Uint8Array([]) });
    channel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId: "unknown" }) });
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "one", size: 1 }) });
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "two", size: 1 }) });
    channel.onmessage({ data: new Uint8Array([1, 2]) });
    expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
    channel.onmessage({
      data: JSON.stringify({ type: "file-start", transferId: "chunks", size: 40_000 })
    });
    mesh.transfers.incomingFiles.get("host").chunkCount = 32_768;
    channel.onmessage({ data: new Uint8Array([1]) });
    expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
  });
  test("handles unavailable Blob construction and peer removal during transfer", async () => {
    const { mesh, channel } = setupChannel();
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "one", size: 1 }) });
    channel.onmessage({ data: new Uint8Array([1]) });
    const BlobClass = globalThis.Blob;
    globalThis.Blob = undefined;
    channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "one" }) });
    globalThis.Blob = BlobClass;
    const rejected = vi.fn();
    mesh.transfers.pendingTransferConfirmations.set("pending", {
      participantId: "guest",
      channel,
      reject: rejected,
      resolve: vi.fn(),
      timer: 1
    });
    mesh.removePeer("guest");
    expect(rejected).toHaveBeenCalledWith(
      expect.objectContaining({
        message: "Участник отключился во время передачи. Отправьте файл заново — продолжить прерванную передачу нельзя."
      })
    );
    expect(mesh.transfers.pendingTransferConfirmations.has("pending")).toBe(false);
  });
  test("cleans detached timers and detects channels closed after waiting", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const clear = vi.spyOn(globalThis, "clearTimeout");
    mesh.disconnectTimers.set("detached", 10);
    mesh.transfers.incomingFiles.set("detached", { timer: 11 });
    mesh.stop();
    expect(clear).toHaveBeenCalledWith(10);
    expect(clear).toHaveBeenCalledWith(11);
    const closed = new FakeChannel("closed");
    mesh.transfers.waitForDataChannel = vi.fn().mockResolvedValue(closed);
    await expect(mesh.sendFile("guest", new Blob([]))).rejects.toThrow("Канал передачи песни закрыт");
  });
  test("clears a channel-owned incoming timer and ignores stale transfer timers", () => {
    vi.useFakeTimers();
    const { mesh, channel } = setupChannel();
    channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "one", size: 1 }) });
    const incomingTimer = mesh.transfers.incomingFiles.get("guest").timer;
    const pendingReject = vi.fn();
    mesh.transfers.pendingTransferConfirmations.set("pending-close", {
      participantId: "guest",
      channel,
      reject: pendingReject,
      resolve: vi.fn(),
      timer: 99
    });
    const clear = vi.spyOn(globalThis, "clearTimeout");
    channel.onclose();
    expect(clear).toHaveBeenCalledWith(incomingTimer);
    expect(clear).toHaveBeenCalledWith(99);
    expect(pendingReject).toHaveBeenCalledWith(expect.objectContaining({ message: "Канал передачи песни закрыт" }));
    expect(mesh.transfers.pendingTransferConfirmations.has("pending-close")).toBe(false);
    mesh.transfers.incomingFiles.set("guest", { metadata: { transferId: "new" }, timer: 1 });
    const staleTimer = mesh.transfers.createIncomingTransferTimer("guest", "old");
    vi.advanceTimersByTime(30_000);
    expect(mesh.transfers.incomingFiles.get("guest").metadata.transferId).toBe("new");
    clearTimeout(staleTimer);
    mesh.transfers.channels.set("guest", new FakeChannel("closed"));
    const expiring = mesh.transfers.createIncomingTransferTimer("guest", "new");
    vi.advanceTimersByTime(30_000);
    clearTimeout(expiring);
  });
  test("preserves unrelated confirmations while removing a peer", () => {
    const mesh = makeMesh();
    const pending = { participantId: "other", reject: vi.fn(), resolve: vi.fn(), timer: 1 };
    mesh.transfers.pendingTransferConfirmations.set("other-transfer", pending);
    mesh.disconnectTimers.set("guest", 22);
    mesh.createPeer("guest");
    mesh.removePeer("guest");
    expect(mesh.transfers.pendingTransferConfirmations.get("other-transfer")).toBe(pending);
  });
  test("clears a pending incoming-file admission timer when its peer disconnects", () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    mesh.createPeer("guest");
    const admissionTimer = globalThis.setTimeout(() => {}, 15_000);
    const progress = vi.fn();
    mesh.onTransferProgress = progress;
    mesh.transfers.incomingFileAdmissions.set("guest", {
      channel: new FakeChannel(),
      cancelled: false,
      timer: admissionTimer,
      metadata: { transferId: "pending" }
    });
    const clear = vi.spyOn(globalThis, "clearTimeout");
    mesh.removePeer("guest");
    expect(clear).toHaveBeenCalledWith(admissionTimer);
    expect(mesh.transfers.incomingFileAdmissions.has("guest")).toBe(false);
    // TASK 5.4: waiting-for-admission transfers must also surface a terminal
    // event, or the sender's caller (syncSong/openKaraokeInRoom) hangs.
    expect(progress).toHaveBeenCalledWith(
      expect.objectContaining({ participantId: "guest", stage: "cancelled", metadata: { transferId: "pending" } })
    );
  });
  test("surfaces a cancelled transfer event when the sending peer disconnects mid-receive", () => {
    // TASK 5.4: the owner of a file we're receiving can disconnect mid-transfer
    // (host push, network drop, tab closed) — removePeer used to wipe the
    // incoming-transfer state silently, leaving OnlineRoomContext's pending
    // song command waiting on an event that would never arrive.
    const mesh = makeMesh();
    const progress = vi.fn();
    mesh.onTransferProgress = progress;
    mesh.createPeer("host");
    mesh.transfers.incomingFiles.set("host", {
      metadata: { transferId: "song-1", commandId: "cmd-1" },
      lastPercent: 42,
      timer: 0,
      sink: { cleanup: vi.fn() }
    });
    mesh.removePeer("host");
    expect(progress).toHaveBeenCalledWith(
      expect.objectContaining({
        participantId: "host",
        stage: "cancelled",
        percent: 42,
        metadata: { transferId: "song-1", commandId: "cmd-1" }
      })
    );
    expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
  });
  test("surfaces a cancelled transfer event when the receiving data channel closes mid-receive", () => {
    const { mesh, channel } = setupChannel("host");
    const progress = vi.fn();
    mesh.onTransferProgress = progress;
    mesh.transfers.incomingFiles.set("host", {
      channel,
      metadata: { transferId: "song-2" },
      lastPercent: 10,
      timer: 0,
      sink: { cleanup: vi.fn() }
    });
    channel.onclose();
    expect(progress).toHaveBeenCalledWith(expect.objectContaining({ participantId: "host", stage: "cancelled", percent: 10 }));
  });
  test("cancels a file transfer when its channel closes after the last chunk", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    const defaultSend = channel.send.getMockImplementation();
    channel.send.mockImplementation((payload) => {
      const result = defaultSend(payload);
      if (payload instanceof ArrayBuffer) channel.readyState = "closed";
      return result;
    });
    mesh.setupDataChannel("guest", channel);
    await expect(mesh.sendFile("guest", new Blob(["x"]))).rejects.toThrow("Передача файла отменена");
  });
  test("cancels during backpressure and after asynchronously reading a chunk", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const channel = new FakeChannel();
    channel.bufferedAmount = 600 * 1024;
    mesh.setupDataChannel("guest", channel);
    const pressured = mesh.sendFile("guest", new Blob(["x"]));
    await Promise.resolve();
    channel.bufferedAmount = 0;
    channel.readyState = "closed";
    const pressureResult = expect(pressured).rejects.toThrow("Передача файла отменена");
    await vi.advanceTimersByTimeAsync(20);
    await pressureResult;
    vi.useRealTimers();
    const nextMesh = makeMesh();
    const nextChannel = new FakeChannel();
    nextMesh.setupDataChannel("guest", nextChannel);
    class ClosingBlob extends Blob {
      slice() {
        return {
          arrayBuffer: async () => {
            nextChannel.readyState = "closed";
            return new ArrayBuffer(1);
          }
        };
      }
    }
    await expect(nextMesh.sendFile("guest", new ClosingBlob(["x"]))).rejects.toThrow("Передача файла отменена");
  });
  test("aborts a transfer whose receiver never drains backpressure", async () => {
    vi.useFakeTimers();
    const mesh = makeMesh();
    const channel = new FakeChannel();
    channel.bufferedAmount = 600 * 1024;
    mesh.setupDataChannel("guest", channel);
    const sending = mesh.sendFile("guest", new Blob(["x"]));
    const result = expect(sending).rejects.toThrow("Передача песни остановилась: нет ответа от участника");
    await vi.advanceTimersByTimeAsync(30_020);
    await result;
  });
  test("surfaces current invitation failures and rejects missing local offers", async () => {
    globalThis.RTCPeerConnection = class FailingOfferPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.createOffer = vi.fn().mockRejectedValue(new Error("offer failed"));
      }
    };
    await expect(makeMesh().invite("guest")).rejects.toThrow("offer failed");
    globalThis.RTCPeerConnection = class MissingLocalPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.setLocalDescription = vi.fn().mockResolvedValue(undefined);
      }
    };
    await expect(makeMesh().invite("guest")).resolves.toBe(false);
    let rejectOffer;
    globalThis.RTCPeerConnection = class StaleFailingOfferPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.createOffer = vi.fn(
          () =>
            new Promise((_resolve, reject) => {
              rejectOffer = reject;
            })
        );
      }
    };
    const staleMesh = makeMesh();
    const staleInvite = staleMesh.invite("guest");
    staleMesh.removePeer("guest");
    rejectOffer(new Error("stale offer"));
    await expect(staleInvite).resolves.toBe(false);
  });
  test("cancels from inside a backpressured transfer loop", async () => {
    const mesh = makeMesh();
    const channel = new FakeChannel();
    channel.bufferedAmount = 600 * 1024;
    channel.send.mockImplementationOnce(() => {
      mesh.lifecycleVersion += 1;
    });
    mesh.setupDataChannel("guest", channel);
    await expect(mesh.sendFile("guest", new Blob(["x"]))).rejects.toThrow("Передача файла отменена");
  });
  test("cancels queued and direct ICE signals for replaced peers", async () => {
    const mesh = makeMesh();
    let releasePrevious;
    mesh.signalPromises.set(
      "queued",
      new Promise((resolve) => {
        releasePrevious = resolve;
      })
    );
    const queued = mesh.accept("queued", { candidate: "ice" });
    mesh.removePeer("queued");
    releasePrevious();
    await expect(queued).resolves.toBe(false);
    const signal = {};
    Object.defineProperty(signal, "candidate", {
      get() {
        mesh.removePeer("direct");
        return "ice";
      }
    });
    await expect(mesh.accept("direct", signal)).resolves.toBe(false);
  });
  test("cancels description, candidate replay and answer work for replaced peers", async () => {
    let resolveRemote;
    globalThis.RTCPeerConnection = class DeferredRemotePeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.setRemoteDescription = vi.fn(
          () =>
            new Promise((resolve) => {
              resolveRemote = resolve;
            })
        );
      }
    };
    let mesh = makeMesh();
    const remote = mesh.accept("remote", { description: { type: "offer" } });
    await Promise.resolve();
    await Promise.resolve();
    mesh.removePeer("remote");
    resolveRemote();
    await expect(remote).resolves.toBe(false);
    let resolveIce;
    globalThis.RTCPeerConnection = class DeferredIcePeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.addIceCandidate = vi.fn(
          () =>
            new Promise((resolve) => {
              resolveIce = resolve;
            })
        );
      }
    };
    mesh = makeMesh();
    await mesh.accept("ice", { candidate: "one" });
    await mesh.accept("ice", { candidate: "two" });
    const replay = mesh.accept("ice", { description: { type: "answer" } });
    await vi.waitFor(() => expect(resolveIce).toBeTypeOf("function"));
    mesh.removePeer("ice");
    resolveIce();
    await expect(replay).resolves.toBe(false);
    let resolveAnswer;
    globalThis.RTCPeerConnection = class DeferredAnswerPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.createAnswer = vi.fn(
          () =>
            new Promise((resolve) => {
              resolveAnswer = resolve;
            })
        );
      }
    };
    mesh = makeMesh();
    const answer = mesh.accept("answer", { description: { type: "offer" } });
    await vi.waitFor(() => expect(resolveAnswer).toBeTypeOf("function"));
    mesh.removePeer("answer");
    resolveAnswer({ type: "answer" });
    await expect(answer).resolves.toBe(false);
    globalThis.RTCPeerConnection = class MissingLocalAnswerPeer extends FakePeer {
      constructor(configuration) {
        super(configuration);
        this.setLocalDescription = vi.fn().mockResolvedValue(undefined);
      }
    };
    await expect(makeMesh().accept("missing", { description: { type: "offer" } })).resolves.toBe(false);
  });
});
test("peer removal and mesh stop clean incoming transfer sinks exactly once", async () => {
  const mesh = makeMesh();
  const removedCleanup = vi.fn().mockResolvedValue();
  mesh.transfers.incomingFiles.set("guest", { timer: 0, sink: { cleanup: removedCleanup } });
  mesh.removePeer("guest");
  await Promise.resolve();
  expect(removedCleanup).toHaveBeenCalledTimes(1);
  const stoppedCleanup = vi.fn().mockResolvedValue();
  mesh.transfers.incomingFiles.set("other", { timer: 0, sink: { cleanup: stoppedCleanup } });
  mesh.stop();
  await Promise.resolve();
  expect(stoppedCleanup).toHaveBeenCalledTimes(1);
});
test("receiver write credits bound sender in-flight payload to the advertised window", async () => {
  const mesh = makeMesh();
  const channel = new FakeChannel();
  channel.autoCredit = false;
  mesh.setupDataChannel("guest", channel);
  mesh.transfers.waitForDataChannel = vi.fn().mockResolvedValue(channel);
  const transfer = mesh.sendFile("guest", new Blob([new Uint8Array(1024 * 1024)]));
  await vi.waitFor(() => {
    const chunks = channel.send.mock.calls.filter(([value]) => value instanceof ArrayBuffer);
    expect(chunks).toHaveLength(16);
  });
  await new Promise((resolve) => setTimeout(resolve, 5));
  expect(channel.send.mock.calls.filter(([value]) => value instanceof ArrayBuffer)).toHaveLength(16);
  const start = channel.send.mock.calls
    .map(([value]) => (typeof value === "string" ? JSON.parse(value) : null))
    .find((value) => value?.type === "file-start");
  for (let i = 0; i < 16; i += 1) {
    channel.onmessage({
      data: JSON.stringify({ type: "file-credit", transferId: start.transferId, bytes: 32 * 1024 })
    });
  }
  await vi.waitFor(() => expect(channel.send.mock.calls.filter(([value]) => value instanceof ArrayBuffer)).toHaveLength(32));
  for (let i = 0; i < 16; i += 1) {
    channel.onmessage({
      data: JSON.stringify({ type: "file-credit", transferId: start.transferId, bytes: 32 * 1024 })
    });
  }
  await vi.waitFor(() => {
    const end = channel.send.mock.calls
      .map(([value]) => (typeof value === "string" ? JSON.parse(value) : null))
      .find((value) => value?.type === "file-end");
    expect(end).toBeTruthy();
  });
  channel.onmessage({
    data: JSON.stringify({ type: "file-complete", transferId: start.transferId })
  });
  await expect(transfer).resolves.toBeUndefined();
});
test("channel close cancels a credit waiter immediately", async () => {
  const mesh = makeMesh();
  const channel = new FakeChannel();
  channel.autoCredit = false;
  mesh.setupDataChannel("guest", channel);
  mesh.transfers.waitForDataChannel = vi.fn().mockResolvedValue(channel);
  const transfer = mesh.sendFile("guest", new Blob([new Uint8Array(1024 * 1024)]));
  await vi.waitFor(() => expect(mesh.transfers.pendingTransferCredits.size).toBe(1));
  await vi.waitFor(() => expect(channel.send.mock.calls.filter(([value]) => value instanceof ArrayBuffer)).toHaveLength(16));
  channel.readyState = "closed";
  channel.onclose();
  await expect(transfer).rejects.toThrow("Канал передачи песни закрыт");
  expect(mesh.transfers.pendingTransferCredits.size).toBe(0);
});
test("channel replacement cancels admission, credit and confirmation owned by the old channel", async () => {
  const makeTransfer = async (phase) => {
    const mesh = makeMesh();
    const oldChannel = new FakeChannel();
    const nextChannel = new FakeChannel();
    if (phase === "admission") oldChannel.autoReady = false;
    if (phase === "credit") oldChannel.autoCredit = false;
    mesh.setupDataChannel("guest", oldChannel);
    mesh.transfers.waitForDataChannel = vi.fn().mockResolvedValue(oldChannel);
    const size = phase === "credit" ? 1024 * 1024 : 1;
    const transfer = mesh.sendFile("guest", new Blob([new Uint8Array(size)]));
    const store =
      phase === "admission"
        ? mesh.transfers.pendingTransferAdmissions
        : phase === "credit"
          ? mesh.transfers.pendingTransferCredits
          : mesh.transfers.pendingTransferConfirmations;
    await vi.waitFor(() => expect(store.size).toBe(1));
    mesh.setupDataChannel("guest", nextChannel);
    await expect(transfer).rejects.toThrow("Канал передачи песни закрыт");
    expect(store.size).toBe(0);
  };
  await makeTransfer("admission");
  await makeTransfer("credit");
  await makeTransfer("confirmation");
});
test("stop rejects orphan outbound transfer registries instead of clearing them silently", () => {
  const mesh = makeMesh();
  const rejectAdmission = vi.fn();
  const rejectConfirmation = vi.fn();
  const rejectCredit = vi.fn();
  const timer = setTimeout(() => {}, 60_000);
  mesh.transfers.pendingTransferAdmissions.set("a", {
    participantId: "gone",
    channel: {},
    timer,
    reject: rejectAdmission
  });
  mesh.transfers.pendingTransferConfirmations.set("b", {
    participantId: "gone",
    channel: {},
    timer,
    reject: rejectConfirmation
  });
  mesh.transfers.pendingTransferCredits.set("c", {
    participantId: "gone",
    channel: {},
    waiters: [{ timer, reject: rejectCredit }]
  });
  mesh.stop();
  expect(rejectAdmission).toHaveBeenCalledOnce();
  expect(rejectConfirmation).toHaveBeenCalledOnce();
  expect(rejectCredit).toHaveBeenCalledOnce();
  expect(mesh.transfers.pendingTransferAdmissions.size).toBe(0);
  expect(mesh.transfers.pendingTransferConfirmations.size).toBe(0);
  expect(mesh.transfers.pendingTransferCredits.size).toBe(0);
});
test("busy receiver rejects file-start immediately", () => {
  const { mesh, channel } = setupChannel("host");
  mesh.transfers.incomingFileAdmissions.set("host", { channel, transferId: "existing", cancelled: false });
  channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "new", size: 1 }) });
  expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"type":"file-error"'));
  expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"transferId":"new"'));
});
test("stale old-channel close never cancels a transfer owned by its replacement", async () => {
  const mesh = makeMesh();
  const oldChannel = new FakeChannel();
  const nextChannel = new FakeChannel();
  mesh.setupDataChannel("guest", oldChannel);
  mesh.setupDataChannel("guest", nextChannel);
  mesh.transfers.waitForDataChannel = vi.fn().mockResolvedValue(nextChannel);
  const transfer = mesh.sendFile("guest", new Blob([new Uint8Array(1)]));
  await vi.waitFor(() => expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1));
  oldChannel.onclose?.();
  expect(mesh.transfers.pendingTransferConfirmations.size).toBe(1);
  const transferId = [...mesh.transfers.pendingTransferConfirmations.keys()][0];
  nextChannel.onmessage({ data: JSON.stringify({ type: "file-complete", transferId }) });
  await expect(transfer).resolves.toBeUndefined();
});
test("stale old-channel binary never mutates the replacement incoming transfer", () => {
  const mesh = makeMesh();
  const oldChannel = new FakeChannel();
  const nextChannel = new FakeChannel();
  mesh.setupDataChannel("host", oldChannel);
  oldChannel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "old", size: 1 })
  });
  mesh.setupDataChannel("host", nextChannel);
  nextChannel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "new", size: 1 })
  });
  const transfer = mesh.transfers.incomingFiles.get("host");
  const write = vi.spyOn(transfer.sink, "write");
  oldChannel.onmessage({ data: new Uint8Array([7]).buffer });
  expect(mesh.transfers.incomingFiles.get("host")).toBe(transfer);
  expect(transfer.received).toBe(0);
  expect(write).not.toHaveBeenCalled();
  expect(oldChannel.send).not.toHaveBeenCalledWith(expect.stringContaining('"type":"file-credit"'));
});
test("stale old-channel file-end never destroys the replacement incoming transfer", () => {
  const mesh = makeMesh();
  const oldChannel = new FakeChannel();
  const nextChannel = new FakeChannel();
  mesh.setupDataChannel("host", oldChannel);
  oldChannel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "old", size: 1 })
  });
  mesh.setupDataChannel("host", nextChannel);
  nextChannel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "new", size: 1 })
  });
  const transfer = mesh.transfers.incomingFiles.get("host");
  const cleanup = vi.spyOn(transfer.sink, "cleanup");
  oldChannel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "old" }) });
  expect(mesh.transfers.incomingFiles.get("host")).toBe(transfer);
  expect(cleanup).not.toHaveBeenCalled();
});
test("invalid current-channel chunk rejects immediately and cleans the transfer once", async () => {
  const { mesh, channel } = setupChannel("host");
  channel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "oversized", size: 1 })
  });
  const transfer = mesh.transfers.incomingFiles.get("host");
  const cleanup = vi.spyOn(transfer.sink, "cleanup");
  channel.send.mockClear();
  channel.onmessage({ data: new Uint8Array([1, 2]).buffer });
  await Promise.resolve();
  expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
  expect(cleanup).toHaveBeenCalledOnce();
  expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"type":"file-error"'));
  expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"transferId":"oversized"'));
});
test("channel replacement cancels pending incoming admission without blocking the new channel", async () => {
  const mesh = makeMesh();
  let resolveOld;
  mesh.canAcceptFile = vi.fn((_participantId, metadata) =>
    metadata.transferId === "old"
      ? new Promise((resolve) => {
          resolveOld = resolve;
        })
      : true
  );
  const oldChannel = new FakeChannel();
  const nextChannel = new FakeChannel();
  mesh.setupDataChannel("host", oldChannel);
  oldChannel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "old", size: 1 })
  });
  await vi.waitFor(() => expect(mesh.transfers.incomingFileAdmissions.has("host")).toBe(true));
  mesh.setupDataChannel("host", nextChannel);
  expect(mesh.transfers.incomingFileAdmissions.has("host")).toBe(false);
  nextChannel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "new", size: 1 })
  });
  await vi.waitFor(() => expect(mesh.transfers.incomingFiles.get("host")?.metadata.transferId).toBe("new"));
  resolveOld(true);
  await Promise.resolve();
  await Promise.resolve();
  expect(mesh.transfers.incomingFiles.get("host")?.metadata.transferId).toBe("new");
});
test("file-end keeps ownership while the final disk write is pending and channel close cancels it", async () => {
  let resolveWrite;
  const pendingWrite = new Promise((resolve) => {
    resolveWrite = resolve;
  });
  const removeEntry = vi.fn().mockResolvedValue(undefined);
  const writable = {
    write: vi.fn(() => pendingWrite),
    close: vi.fn().mockResolvedValue(undefined),
    abort: vi.fn().mockResolvedValue(undefined)
  };
  globalThis.navigator.storage = {
    getDirectory: vi.fn().mockResolvedValue({
      getFileHandle: vi.fn().mockResolvedValue({
        createWritable: vi.fn().mockResolvedValue(writable),
        getFile: vi.fn().mockResolvedValue(new Blob([[1]]))
      }),
      removeEntry
    })
  };
  const mesh = makeMesh();
  const channel = new FakeChannel();
  mesh.onFile = vi.fn().mockResolvedValue(true);
  mesh.setupDataChannel("host", channel);
  channel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "pending-write", size: 1 })
  });
  await vi.waitFor(() => expect(mesh.transfers.incomingFiles.has("host")).toBe(true));
  channel.onmessage({ data: new Uint8Array([1]).buffer });
  await vi.waitFor(() => expect(writable.write).toHaveBeenCalledOnce());
  channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "pending-write" }) });
  expect(mesh.transfers.incomingFiles.get("host")?.phase).toBe("flushing");
  channel.readyState = "closed";
  channel.onclose();
  expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
  await vi.waitFor(() => expect(writable.abort).toHaveBeenCalledOnce());
  await vi.waitFor(() => expect(removeEntry).toHaveBeenCalledOnce());
  expect(mesh.onFile).not.toHaveBeenCalled();
  resolveWrite();
});
test("mesh stop cancels post-file-end finalization before application handoff", async () => {
  let resolveWrite;
  const pendingWrite = new Promise((resolve) => {
    resolveWrite = resolve;
  });
  const writable = {
    write: vi.fn(() => pendingWrite),
    close: vi.fn().mockResolvedValue(undefined),
    abort: vi.fn().mockResolvedValue(undefined)
  };
  globalThis.navigator.storage = {
    getDirectory: vi.fn().mockResolvedValue({
      getFileHandle: vi.fn().mockResolvedValue({
        createWritable: vi.fn().mockResolvedValue(writable),
        getFile: vi.fn().mockResolvedValue(new Blob([[1]]))
      }),
      removeEntry: vi.fn().mockResolvedValue(undefined)
    })
  };
  const mesh = makeMesh();
  const channel = new FakeChannel();
  mesh.onFile = vi.fn().mockResolvedValue(true);
  mesh.setupDataChannel("host", channel);
  channel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "stop-finalize", size: 1 })
  });
  await vi.waitFor(() => expect(mesh.transfers.incomingFiles.has("host")).toBe(true));
  channel.onmessage({ data: new Uint8Array([1]).buffer });
  await vi.waitFor(() => expect(writable.write).toHaveBeenCalledOnce());
  channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "stop-finalize" }) });
  mesh.stop();
  await vi.waitFor(() => expect(writable.abort).toHaveBeenCalledOnce());
  expect(mesh.transfers.incomingFiles.size).toBe(0);
  expect(mesh.onFile).not.toHaveBeenCalled();
  resolveWrite();
});
test("receiver stays busy until finalization completes", async () => {
  let resolveImport;
  const mesh = makeMesh();
  const channel = new FakeChannel();
  mesh.onFile = vi.fn(
    () =>
      new Promise((resolve) => {
        resolveImport = resolve;
      })
  );
  mesh.setupDataChannel("host", channel);
  channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "one", size: 1 }) });
  channel.onmessage({ data: new Uint8Array([1]).buffer });
  channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "one" }) });
  await vi.waitFor(() => expect(mesh.transfers.incomingFiles.get("host")?.phase).toBe("importing"));
  channel.send.mockClear();
  channel.onmessage({ data: JSON.stringify({ type: "file-start", transferId: "two", size: 1 }) });
  expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"type":"file-error"'));
  expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"transferId":"two"'));
  resolveImport(true);
  await vi.waitFor(() => expect(mesh.transfers.incomingFiles.has("host")).toBe(false));
});
test("finalization timeout cleans a stalled transfer", async () => {
  vi.useFakeTimers();
  const writable = {
    write: vi.fn(() => new Promise(() => {})),
    close: vi.fn().mockResolvedValue(undefined),
    abort: vi.fn().mockResolvedValue(undefined)
  };
  globalThis.navigator.storage = {
    getDirectory: vi.fn().mockResolvedValue({
      getFileHandle: vi.fn().mockResolvedValue({
        createWritable: vi.fn().mockResolvedValue(writable),
        getFile: vi.fn().mockResolvedValue(new Blob([[1]]))
      }),
      removeEntry: vi.fn().mockResolvedValue(undefined)
    })
  };
  const { mesh, channel } = setupChannel("host");
  channel.onmessage({
    data: JSON.stringify({ type: "file-start", transferId: "timeout-finalize", size: 1 })
  });
  await vi.runAllTicks();
  for (let index = 0; index < 8; index += 1) await Promise.resolve();
  expect(mesh.transfers.incomingFiles.has("host")).toBe(true);
  channel.onmessage({ data: new Uint8Array([1]).buffer });
  await Promise.resolve();
  channel.onmessage({ data: JSON.stringify({ type: "file-end", transferId: "timeout-finalize" }) });
  expect(mesh.transfers.incomingFiles.has("host")).toBe(true);
  await vi.advanceTimersByTimeAsync(30_000);
  expect(mesh.transfers.incomingFiles.has("host")).toBe(false);
  expect(writable.abort).toHaveBeenCalledOnce();
  expect(channel.send).toHaveBeenCalledWith(expect.stringContaining('"type":"file-error"'));
});

test("logs ICE and connection state transitions for diagnostics", () => {
  const info = vi.spyOn(console, "info").mockImplementation(() => {});
  const mesh = makeMesh();
  const peer = mesh.createPeer("guest");

  peer.iceConnectionState = "checking";
  peer.oniceconnectionstatechange();
  expect(info).toHaveBeenCalledWith(
    "WebRTC ICE state changed",
    expect.objectContaining({ participantId: "guest", iceConnectionState: "checking" })
  );

  peer.connectionState = "connected";
  peer.onconnectionstatechange();
  expect(info).toHaveBeenCalledWith(
    "WebRTC connection state changed",
    expect.objectContaining({ participantId: "guest", connectionState: "connected" })
  );
});

test("logs data channel open, close and error transitions for diagnostics", () => {
  const info = vi.spyOn(console, "info").mockImplementation(() => {});
  const error = vi.spyOn(console, "error").mockImplementation(() => {});
  const { channel } = setupChannel("guest");
  channel.label = "karaoke-library";

  channel.onopen();
  expect(info).toHaveBeenCalledWith(
    "WebRTC data channel opened",
    expect.objectContaining({ participantId: "guest", label: "karaoke-library" })
  );

  channel.onclose();
  expect(info).toHaveBeenCalledWith(
    "WebRTC data channel closed",
    expect.objectContaining({ participantId: "guest", label: "karaoke-library" })
  );

  channel.onerror({ error: new Error("boom") });
  expect(error).toHaveBeenCalledWith(
    "WebRTC data channel error",
    expect.objectContaining({ participantId: "guest", label: "karaoke-library" })
  );
});

test("uses the Python monitor relay instead of local capture when it is available", async () => {
  const capture = vi.fn();
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { mediaDevices: { getUserMedia: capture } }
  });
  const graph = fakeRelayGraph();
  relayMocks.createRelayVoiceGraph.mockReset().mockResolvedValue(graph);
  // start() must ask the backend to open the relay (prepareRoomVoiceRelay)
  // before attempting to connect to it -- see tryRelay's comment on why.
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ relay_available: true })
    })
  );

  const mesh = makeMesh();
  let outgoing;
  try {
    outgoing = await mesh.start();
  } finally {
    vi.unstubAllGlobals();
  }

  expect(relayMocks.createRelayVoiceGraph).toHaveBeenCalledWith({ connectTimeoutMs: 1500 });
  expect(capture).not.toHaveBeenCalled();
  expect(mesh.usingRelay).toBe(true);
  expect(mesh.microphoneGraph).toBe(graph);
  expect(outgoing).toBe(graph.stream);
  expect(mesh.stream).toBe(graph.stream);
  expect(mesh.effectsStream).toBe(graph.effectsStream);
  expect(graph.onUnavailable).toHaveBeenCalledWith(expect.any(Function));
});

test("releases a prepared backend relay when browser relay construction fails", async () => {
  const capture = vi.fn().mockResolvedValue(stream([track("mic")]));
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { mediaDevices: { getUserMedia: capture } }
  });
  relayMocks.createRelayVoiceGraph.mockReset().mockRejectedValue(new Error("worklet failed"));
  const fetchMock = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => JSON.stringify({ relay_available: true })
  });
  vi.stubGlobal("fetch", fetchMock);

  try {
    const mesh = makeMesh();
    await mesh.start();
    expect(capture).toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("release-voice-relay"))).toBe(true);
  } finally {
    vi.unstubAllGlobals();
  }
});

test("skips the relay entirely for an ASIO driver and falls back to local capture", async () => {
  const capture = vi.fn().mockResolvedValue(stream([track("mic")]));
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { mediaDevices: { getUserMedia: capture } }
  });
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ audio_driver: "asio" })
    })
  );

  const mesh = makeMesh();
  try {
    await mesh.start();
  } finally {
    vi.unstubAllGlobals();
  }

  expect(relayMocks.createRelayVoiceGraph).not.toHaveBeenCalled();
  expect(capture).toHaveBeenCalled();
  expect(mesh.usingRelay).toBe(false);
});

test("falls back to local capture and re-syncs peers when the relay drops mid-call", async () => {
  const localStream = stream([track("mic")]);
  const capture = vi.fn().mockResolvedValue(localStream);
  Object.defineProperty(globalThis, "navigator", {
    configurable: true,
    value: { mediaDevices: { getUserMedia: capture } }
  });
  const graph = fakeRelayGraph();
  relayMocks.createRelayVoiceGraph
    .mockReset()
    .mockResolvedValueOnce(graph)
    .mockRejectedValue(new Error("relay gone after dropping"));
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ relay_available: true })
    })
  );

  try {
    const mesh = makeMesh();
    await mesh.start();
    expect(mesh.usingRelay).toBe(true);

    const peer = mesh.createPeer("guest");
    const sender = peer.getSenders()[0];
    sender.replaceTrack = vi.fn(async (nextTrack) => {
      sender.track = nextTrack;
    });

    await graph.triggerUnavailable();

    expect(mesh.usingRelay).toBe(false);
    expect(capture).toHaveBeenCalled();
    expect(graph.close).toHaveBeenCalled();
    expect(sender.replaceTrack).toHaveBeenCalledWith(localStream.getAudioTracks()[0]);
  } finally {
    vi.unstubAllGlobals();
  }
});
