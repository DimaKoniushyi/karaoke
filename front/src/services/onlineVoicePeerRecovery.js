// eslint-disable-next-line import/extensions
import { translateSaved } from "../i18n/runtime";
import { updatePeerIceServers } from "./onlineVoicePeerConfiguration";
import { PEER_TIMEOUTS } from "./onlineVoicePeerTimeouts";

export default class OnlineVoicePeerRecovery {
  constructor(mesh, formatOffer) {
    this.mesh = mesh;
    this.formatOffer = formatOffer;
    this.promises = new Map();
    this.recovering = new Set();
  }

  connected(participantId) {
    const timer = this.mesh.disconnectTimers.get(participantId);
    const wasRecovering = this.recovering.delete(participantId);
    if (timer) globalThis.clearTimeout(timer);
    this.mesh.disconnectTimers.delete(participantId);
    if (wasRecovering) this.mesh.onPeerRecovered?.(participantId);
  }

  remove(participantId) {
    this.promises.delete(participantId);
    this.recovering.delete(participantId);
  }

  stop() {
    this.promises.clear();
    this.recovering.clear();
  }

  fail(participantId) {
    const relay = this.mesh.iceServers.some(({ urls }) =>
      (Array.isArray(urls) ? urls : [urls]).some((url) => /^turns?:/.test(url))
    );
    const message = relay
      ? translateSaved("room.couldNotConnectToParticipantVoiceAndSongTransfer")
      : translateSaved("room.directParticipantConnectionFailedAndTurnIsUnconfiguredOr");
    console.error("Room peer connection failed", { participantId, relayAvailable: relay });
    this.mesh.onPeerError?.(participantId, message);
  }

  recover(participantId, peer) {
    const { mesh } = this;
    if (this.promises.has(participantId) || mesh.peers.get(participantId) !== peer)
      return this.promises.get(participantId) || Promise.resolve(false);
    const version = mesh.peerVersions.get(participantId) || 0;
    const isCurrent = () =>
      mesh.peers.get(participantId) === peer &&
      (mesh.peerVersions.get(participantId) || 0) === version &&
      peer.connectionState !== "closed";
    const expiry = globalThis.setTimeout(() => {
      mesh.disconnectTimers.delete(participantId);
      if (!isCurrent() || peer.connectionState === "connected") return;
      this.fail(participantId);
      mesh.removePeer(participantId);
    }, PEER_TIMEOUTS.iceRecovery);
    mesh.disconnectTimers.set(participantId, expiry);
    this.recovering.add(participantId);
    mesh.onPeerRecovering?.(participantId);

    const recovery = this.#restart(participantId, peer, isCurrent)
      .catch((error) => {
        console.error("WebRTC ICE recovery failed", { participantId, error });
        return false;
      })
      .finally(() => {
        if (this.promises.get(participantId) === recovery) this.promises.delete(participantId);
      });
    this.promises.set(participantId, recovery);
    return recovery;
  }

  async #restart(participantId, peer, isCurrent) {
    const { mesh } = this;
    // The original offerer remains the offerer during recovery. This avoids
    // glare when both machines notice the broken path at the same time.
    if (!mesh.peerInitiators.has(participantId)) return true;
    if (mesh.roomClient.getIceServers)
      mesh.iceServers = await mesh.roomClient.getIceServers({ force: true });
    if (!isCurrent()) return false;
    updatePeerIceServers(peer, mesh.iceServers);
    const offer = this.formatOffer(await peer.createOffer({ iceRestart: true }));
    if (!isCurrent()) return false;
    await peer.setLocalDescription(offer);
    if (!isCurrent() || !peer.localDescription) return false;
    return mesh.roomClient.send("signal", {
      targetId: participantId,
      signal: { description: peer.localDescription }
    });
  }
}
