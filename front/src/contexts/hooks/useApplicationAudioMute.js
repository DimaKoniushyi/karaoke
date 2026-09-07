import { useCallback, useEffect, useRef } from "react";

const collectAudioElements = (root) => [
  ...(root instanceof HTMLAudioElement ? [root] : []),
  ...(root.querySelectorAll?.("audio") || [])
];

export default function useApplicationAudioMute(enabled) {
  const originalMuteStateRef = useRef(new Map());

  const muteApplicationAudio = useCallback(
    (root) => {
      if (!root) return;

      for (const audio of collectAudioElements(root)) {
        if (audio.dataset.onlineRoomParticipant) continue;
        if (!originalMuteStateRef.current.has(audio))
          originalMuteStateRef.current.set(audio, audio.muted);
        audio.muted = true;
      }
    },
    // Stryker disable next-line ArrayDeclaration: the callback closes over refs only.
    []
  );

  const forgetRemovedAudio = useCallback((root) => {
    if (!root) return;

    for (const audio of collectAudioElements(root)) originalMuteStateRef.current.delete(audio);
    // Stryker disable next-line ArrayDeclaration: the callback closes over refs only.
  }, []);

  const restoreApplicationAudio = useCallback(
    () => {
      for (const [audio, wasMuted] of originalMuteStateRef.current) {
        if (audio.isConnected) audio.muted = wasMuted;
      }
      originalMuteStateRef.current.clear();
    },
    // Stryker disable next-line ArrayDeclaration: the callback closes over refs only.
    []
  );

  useEffect(() => {
    if (!enabled) return undefined;

    muteApplicationAudio(document);
    if (typeof MutationObserver === "undefined") return restoreApplicationAudio;

    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          muteApplicationAudio(node);
        }
        // KaraokeMedia remounts a fresh <audio>/<video> element per song, so
        // without this the map below would keep one stale entry (pinning a
        // detached DOM node) per removed element for as long as room-sound
        // mute stays enabled across an online-room session.
        for (const node of mutation.removedNodes) {
          forgetRemovedAudio(node);
        }
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => {
      observer.disconnect();
      restoreApplicationAudio();
    };
  }, [enabled, forgetRemovedAudio, muteApplicationAudio, restoreApplicationAudio]);

  useEffect(
    () => restoreApplicationAudio,
    // Stryker disable next-line ArrayDeclaration: restoreApplicationAudio has stable identity.
    [restoreApplicationAudio]
  );

  return { muteApplicationAudio, restoreApplicationAudio };
}
