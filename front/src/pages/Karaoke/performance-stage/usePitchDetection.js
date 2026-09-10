import { useEffect, useState } from "react";
import useHardwareSuspended from "../../../hooks/useHardwareSuspended";
import { acquireMicrophone } from "../../../services/microphoneCapture";
import { closeAudioContext, closeAudioContextQuietly } from "../../../utils/audio-context";
import { safe } from "../utils/async";
import { detectMidiFromAnalyser } from "../utils/pitch";

const IDLE = {
  sungMidi: null,
  isPitchDetected: false,
  isPitchAttacking: false,
  pitchRestProgress: 1
};

export default function usePitchDetection({ isPlaying, monitorInputDeviceId, getLocalVoiceStream }) {
  const suspended = useHardwareSuspended();
  const [pitch, setPitch] = useState(IDLE);

  useEffect(() => {
    const canCapture = getLocalVoiceStream || globalThis.navigator?.mediaDevices?.getUserMedia;
    if (!isPlaying || suspended || !canCapture) {
      setPitch(IDLE);
      return;
    }

    let cancelled = false;
    let frame = 0;
    let lease;
    let context;
    let source;
    let target = null;
    let displayed = null;
    let measuredAt = 0;
    let animatedAt = 0;
    let renderedAt = 0;
    let voicedAt = 0;
    let restAt = 0;
    let attackUntil = 0;
    const recent = [];
    const release = () => safe(() => lease?.release?.());
    const patch = (next) => setPitch((state) => ({ ...state, ...next }));

    setPitch(IDLE);

    const start = async () => {
      try {
        const stream = getLocalVoiceStream
          ? await getLocalVoiceStream()
          : (lease = await acquireMicrophone(monitorInputDeviceId, { disabledEffects: true })).stream;

        if (!stream || cancelled) return release();
        const AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext;
        if (!AudioContext) return release();

        context = new AudioContext({ latencyHint: "interactive" });
        if (context.state === "suspended") await context.resume();
        if (cancelled) {
          await release();
          await closeAudioContext(context);
          return;
        }

        const analyser = context.createAnalyser();
        analyser.fftSize = 2048;
        analyser.smoothingTimeConstant = 0.2;
        source = context.createMediaStreamSource(stream);
        source.connect(analyser);
        const buffer = new Float32Array(analyser.fftSize);

        const update = (now) => {
          if (cancelled) return;

          if (now - measuredAt >= 35) {
            measuredAt = now;
            const midi = detectMidiFromAnalyser(analyser, buffer, context.sampleRate);

            if (Number.isFinite(midi)) {
              recent.push(midi);
              if (recent.length > 3) recent.shift();
              const sorted = [...recent].sort((a, b) => a - b);
              const median = sorted[Math.floor(sorted.length / 2)];
              target = Number.isFinite(target) ? target + (median - target) * 0.42 : median;
              const attacking = restAt > 0 || !Number.isFinite(displayed);

              voicedAt = now;
              restAt = 0;
              if (attacking) {
                displayed = target;
                attackUntil = now + 130;
              }
              patch({
                ...(attacking ? { sungMidi: target, isPitchAttacking: true } : {}),
                isPitchDetected: true,
                pitchRestProgress: 0
              });
            }
          }

          if (now - voicedAt > 110) {
            target = null;
            if (!restAt && Number.isFinite(displayed)) {
              restAt = now;
              patch({ isPitchDetected: false, isPitchAttacking: false });
            }
          }

          if (attackUntil && now >= attackUntil) {
            attackUntil = 0;
            patch({ isPitchAttacking: false });
          }

          if (Number.isFinite(target)) {
            const seconds = Math.min(0.05, Math.max(0.001, (now - animatedAt) / 1000));
            displayed += Math.max(-22 * seconds, Math.min(22 * seconds, target - displayed));
            if (now - renderedAt >= 15) {
              renderedAt = now;
              patch({ sungMidi: displayed });
            }
          } else if (restAt) {
            const progress = Math.min(1, (now - restAt) / 380);
            if (now - renderedAt >= 32) {
              renderedAt = now;
              patch({ pitchRestProgress: progress });
            }
            if (progress >= 1) {
              displayed = null;
              recent.length = 0;
              restAt = 0;
              patch({ sungMidi: null, pitchRestProgress: 1 });
            }
          }

          animatedAt = now;
          frame = requestAnimationFrame(update);
        };

        frame = requestAnimationFrame(update);
      } catch {
        release();
        closeAudioContextQuietly(context);
      }
    };

    start();
    return () => {
      cancelled = true;
      globalThis.cancelAnimationFrame?.(frame);
      try {
        source?.disconnect();
      } catch {}
      release();
      closeAudioContextQuietly(context);
    };
  }, [getLocalVoiceStream, isPlaying, monitorInputDeviceId, suspended]);

  return pitch;
}
