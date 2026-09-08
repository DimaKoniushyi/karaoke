// The overall WebRTC peer reconnect budget for a room call, shared between
// OnlineVoiceMesh (initial connect, disconnect grace) and
// OnlineVoicePeerRecovery (ICE restart) -- kept in one place so the whole
// timing budget can be seen and tuned together instead of three independent
// literals split across two files.
export const PEER_TIMEOUTS = Object.freeze({
  // How long a peer may sit in "connecting" before we give up on it.
  connect: 30_000,
  // Grace period after a peer drops to "disconnected" before recovery kicks
  // in -- ICE briefly reports this during normal network hiccups.
  disconnectGrace: 10_000,
  // How long an ICE restart may take before recovery gives up and removes
  // the peer.
  iceRecovery: 15_000
});
