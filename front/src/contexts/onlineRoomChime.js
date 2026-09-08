import roomJoinLeaveSound from "../assets/sounds/room-join-leave.mp3";
import { getAudioContextClass } from "../utils/audio-context";

const setAudioParam = (parameter, value, time) => {
  if (typeof parameter?.setValueAtTime === "function") parameter.setValueAtTime(value, time);
  else if (parameter) parameter.value = value;
};

const bendAudioParam = (parameter, value, time) => {
  if (typeof parameter?.exponentialRampToValueAtTime === "function")
    parameter.exponentialRampToValueAtTime(value, time);
  else if (parameter) parameter.value = value;
};

// Builds a throwaway AudioContext, lets `build` wire up its oscillator
// graph and return the tone(s) it started, then closes the context once
// every returned tone has fired its `onended` -- shared by every synthesized
// room cue below so each one only supplies its own oscillator/gain wiring.
function withTransientToneContext(build) {
  const AudioContext = getAudioContextClass();
  if (!AudioContext || typeof AudioContext.prototype?.createOscillator !== "function") return false;
  let context;
  try {
    context = new AudioContext({ latencyHint: "interactive" });
    const tones = build(context);
    Promise.all(
      (Array.isArray(tones) ? tones : [tones]).map(
        (tone) =>
          new Promise((resolve) => {
            tone.onended = () => {
              tone.onended = null;
              resolve();
            };
          })
      )
    )
      .then(() => context.close?.())
      .catch(() => undefined);
    context.resume?.()?.catch?.(() => undefined);
    return true;
  } catch {
    Promise.resolve(context?.close?.()).catch(() => undefined);
    return false;
  }
}

function playSynthesizedRoomSound(direction) {
  return withTransientToneContext((context) => {
    const gain = context.createGain();
    const { currentTime } = context;
    gain.gain.setValueAtTime(0.0001, currentTime);
    gain.gain.exponentialRampToValueAtTime(0.038, currentTime + 0.012);
    gain.gain.exponentialRampToValueAtTime(0.014, currentTime + 0.19);
    gain.gain.exponentialRampToValueAtTime(0.0001, currentTime + 0.42);

    const filter = context.createBiquadFilter?.();
    if (filter) {
      filter.type = "lowpass";
      setAudioParam(filter.frequency, 3200, currentTime);
      setAudioParam(filter.Q, 1.3, currentTime);
      gain.connect(filter);
      filter.connect(context.destination);
    } else gain.connect(context.destination);

    const notes =
      direction === "leave"
        ? [
            [783.99, 659.25, 0],
            [587.33, 440, 0.11]
          ]
        : [
            [493.88, 659.25, 0],
            [659.25, 987.77, 0.11]
          ];
    const wave = context.createPeriodicWave?.(
      new Float32Array(7),
      new Float32Array([0, 1, 0.52, 0.31, 0.17, 0.09, 0.04])
    );
    const tones = notes.map(([startFrequency, endFrequency, offset], index) => {
      const tone = context.createOscillator();
      if (wave && typeof tone.setPeriodicWave === "function") tone.setPeriodicWave(wave);
      else tone.type = index ? "triangle" : "sawtooth";
      setAudioParam(tone.frequency, startFrequency, currentTime + offset);
      bendAudioParam(tone.frequency, endFrequency, currentTime + offset + 0.19);
      setAudioParam(tone.detune, index ? -4 : 3, currentTime + offset);
      tone.connect(gain);
      tone.start(currentTime + offset);
      tone.stop(currentTime + 0.4);
      return tone;
    });
    return tones;
  });
}

function playRoomSound(direction) {
  if (typeof globalThis.Audio !== "function") return playSynthesizedRoomSound(direction);
  try {
    const audio = new globalThis.Audio(roomJoinLeaveSound);
    audio.preload = "auto";
    audio.volume = 0.18;
    audio.playbackRate = 1;
    const release = () => {
      audio.onended = null;
      audio.onerror = null;
      audio.src = "";
    };
    audio.onended = release;
    audio.onerror = release;
    Promise.resolve(audio.play()).catch(() => {
      release();
      playSynthesizedRoomSound(direction);
    });
    return true;
  } catch {
    return playSynthesizedRoomSound(direction);
  }
}

// An unexpected drop (not a voluntary leaveRoom()) gets its own, more
// mournful cue -- a descending "wah-wah" horn, in the spirit of the classic
// voice-chat disconnect sound, played entirely from oscillators (no sampled
// audio, so there is nothing here that could carry someone else's recording).
function playDisconnectHornSound() {
  return withTransientToneContext((context) => {
    const master = context.createGain();
    master.gain.setValueAtTime(0.09, context.currentTime);
    master.connect(context.destination);

    const wave = context.createPeriodicWave?.(
      new Float32Array(6),
      new Float32Array([0, 1, 0.46, 0.27, 0.14, 0.06])
    );
    // Two brassy "wah" swoops, the second lower and slower -- the descending
    // pair reads as sadder than either one alone.
    const swoops = [
      { start: 311.13, end: 220.0, at: 0, duration: 0.34 },
      { start: 246.94, end: 146.83, at: 0.32, duration: 0.55 }
    ];
    const tones = [];
    for (const { start, end, at, duration } of swoops) {
      const beginAt = context.currentTime + at;
      const endAt = beginAt + duration;

      const tone = context.createOscillator();
      if (wave && typeof tone.setPeriodicWave === "function") tone.setPeriodicWave(wave);
      else tone.type = "sawtooth";
      setAudioParam(tone.frequency, start, beginAt);
      bendAudioParam(tone.frequency, end, endAt);

      const filter = context.createBiquadFilter?.();
      const envelope = context.createGain();
      envelope.gain.setValueAtTime(0.0001, beginAt);
      envelope.gain.exponentialRampToValueAtTime(1, beginAt + 0.04);
      envelope.gain.exponentialRampToValueAtTime(0.0001, endAt);

      if (filter) {
        filter.type = "lowpass";
        setAudioParam(filter.frequency, 1400, beginAt);
        bendAudioParam(filter.frequency, 420, endAt);
        setAudioParam(filter.Q, 2.2, beginAt);
        tone.connect(filter);
        filter.connect(envelope);
      } else {
        tone.connect(envelope);
      }
      envelope.connect(master);

      tone.start(beginAt);
      tone.stop(endAt + 0.05);
      tones.push(tone);
    }
    return tones;
  });
}

export const playParticipantJoinedSound = () => playRoomSound("join");
export const playParticipantLeftSound = () => playRoomSound("leave");
export const playConnectionLostSound = () => playDisconnectHornSound();
