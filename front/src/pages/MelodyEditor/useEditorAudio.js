import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { translateSaved as t } from "../../i18n/runtime";
import { getErrorMessage } from "../../utils/errors";
import { clamp } from "../../utils/math";
import { midiToFrequency } from "../../utils/music";
import { setPlayheadPosition } from "./model";

const frequency = (midi) => midiToFrequency(midi) ?? 0;

const noteAt = (notes, time) => {
  let low = 0;
  let high = notes.length - 1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    const note = notes[middle];
    if (time < note.start) high = middle - 1;
    else if (time >= note.end) low = middle + 1;
    else return note;
  }
  return null;
};

export default function useEditorAudio({
  autoScroll,
  duration,
  keyboardWidth,
  notes,
  notify,
  playbackRate,
  playheadRef,
  shellRef,
  songId,
  volumes,
  zoom
}) {
  const instrumentalRef = useRef(null);
  const vocalsRef = useRef(null);
  const contextRef = useRef(null);
  const oscillatorRef = useRef(null);
  const toneTimerRef = useRef(null);
  const timeRef = useRef(0);
  const [urls, setUrls] = useState({});
  const [playing, setPlaying] = useState(false);
  const [time, setTime] = useState(0);
  const orderedNotes = useMemo(
    () => [...notes].sort((left, right) => left.start - right.start || left.end - right.end),
    [notes]
  );

  useEffect(() => {
    if (!songId) return undefined;
    let active = true;
    const resources = [];
    Promise.all([
      api.getAudioTrackBlob(songId, "vocals"),
      api.getAudioTrackBlob(songId, "instrumental")
    ])
      .then(([vocals, instrumental]) => {
        if (!active) return;
        const next = {
          vocals: URL.createObjectURL(vocals),
          instrumental: URL.createObjectURL(instrumental)
        };
        resources.push(next.vocals, next.instrumental, vocals, instrumental);
        setUrls(next);
      })
      .catch(() => {});
    return () => {
      active = false;
      resources.forEach((resource) =>
        typeof resource === "string" ? URL.revokeObjectURL(resource) : resource?.cleanup?.()
      );
    };
  }, [songId]);

  const stopTone = useCallback(() => {
    globalThis.clearTimeout(toneTimerRef.current);
    toneTimerRef.current = null;
    try {
      oscillatorRef.current?.oscillator.stop();
    } catch {
      // The browser may have stopped this oscillator when its scheduled note ended.
    }
    oscillatorRef.current = null;
  }, []);

  const tone = useCallback(
    (midi, milliseconds = 160) => {
      stopTone();
      const AudioContext = globalThis.AudioContext || globalThis.webkitAudioContext;
      if (!AudioContext) return;
      const context = contextRef.current || new AudioContext();
      contextRef.current = context;
      const oscillator = context.createOscillator();
      const gain = context.createGain();
      oscillator.frequency.value = frequency(midi);
      oscillator.type = "sine";
      gain.gain.value = Math.max(0.001, volumes.melody * 0.16);
      oscillator.connect(gain).connect(context.destination);
      oscillator.start();
      oscillatorRef.current = { oscillator, midi };
      toneTimerRef.current = globalThis.setTimeout(() => {
        if (oscillatorRef.current?.oscillator === oscillator) stopTone();
      }, milliseconds);
    },
    [stopTone, volumes.melody]
  );

  useEffect(() => {
    const instrumental = instrumentalRef.current;
    const vocals = vocalsRef.current;
    if (instrumental) {
      instrumental.volume = volumes.instrumental;
      instrumental.playbackRate = playbackRate;
    }
    if (vocals) {
      vocals.volume = volumes.vocals;
      vocals.playbackRate = playbackRate;
    }
  }, [playbackRate, volumes]);

  const followPlayheadRef = useRef(false);
  useEffect(() => {
    const updateFollow = (event) => {
      followPlayheadRef.current = event.ctrlKey && event.shiftKey;
    };
    const stopFollow = () => {
      followPlayheadRef.current = false;
    };
    window.addEventListener("keydown", updateFollow);
    window.addEventListener("keyup", updateFollow);
    window.addEventListener("blur", stopFollow);
    return () => {
      window.removeEventListener("keydown", updateFollow);
      window.removeEventListener("keyup", updateFollow);
      window.removeEventListener("blur", stopFollow);
    };
  }, []);

  useEffect(() => {
    let frame;
    let lastScroll = 0;
    let lastUpdate = 0;
    let activeMidi = null;
    const tick = (stamp) => {
      const master = instrumentalRef.current;
      const vocals = vocalsRef.current;
      if (followPlayheadRef.current && shellRef.current) {
        const shell = shellRef.current;
        const x = keyboardWidth + timeRef.current * zoom;
        const maxScroll = shell.scrollWidth - shell.clientWidth;
        shell.scrollLeft = clamp(x - shell.clientWidth / 2, 0, maxScroll);
      }
      if (master && !master.paused) {
        const current = master.currentTime;
        timeRef.current = current;
        setPlayheadPosition(playheadRef, current, zoom);
        if (vocals && Math.abs(vocals.currentTime - current) > 0.08) vocals.currentTime = current;
        const note = noteAt(orderedNotes, current);
        if (note?.note !== activeMidi) {
          activeMidi = note?.note ?? null;
          if (activeMidi == null) stopTone();
          else tone(activeMidi, Math.max(80, (note.end - current) * 1000));
        }
        if (autoScroll && shellRef.current && stamp - lastScroll >= 100) {
          lastScroll = stamp;
          const shell = shellRef.current;
          const x = keyboardWidth + current * zoom;
          if (x > shell.scrollLeft + shell.clientWidth * 0.72)
            shell.scrollLeft = Math.max(0, x - shell.clientWidth * 0.35);
        }
        if (stamp - lastUpdate >= 125) {
          lastUpdate = stamp;
          setTime(current);
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [autoScroll, keyboardWidth, orderedNotes, playheadRef, shellRef, stopTone, tone, zoom]);

  const seek = useCallback(
    (value) => {
      const next = clamp(Number(value) || 0, 0, duration);
      [instrumentalRef.current, vocalsRef.current].forEach((audio) => {
        if (audio) audio.currentTime = next;
      });
      timeRef.current = next;
      setPlayheadPosition(playheadRef, next, zoom);
      setTime(next);
      const note = noteAt(orderedNotes, next);
      if (!playing && note) tone(note.note);
    },
    [duration, orderedNotes, playheadRef, playing, tone, zoom]
  );

  const pause = useCallback(() => {
    instrumentalRef.current?.pause();
    vocalsRef.current?.pause();
    stopTone();
    setPlaying(false);
  }, [stopTone]);

  const play = useCallback(async () => {
    const master = instrumentalRef.current;
    const vocals = vocalsRef.current;
    if (!master || !vocals) return;
    try {
      vocals.currentTime = master.currentTime;
      const results = await Promise.allSettled([master.play(), vocals.play()]);
      const failure = results.find(({ status }) => status === "rejected");
      if (failure) throw failure.reason;
      setPlaying(true);
    } catch (error) {
      setPlaying(false);
      await notify(t("editor.failedToStartPlayback", { 0: getErrorMessage(error) }));
    }
  }, [notify]);

  useEffect(
    () => () => {
      stopTone();
      contextRef.current?.close?.();
    },
    [stopTone]
  );

  return { instrumentalRef, pause, play, playing, seek, time, timeRef, tone, urls, vocalsRef };
}
