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
import { registerLightingSource } from "../services/keyboardLighting";
import { createLevelMeter } from "../services/levelMeter";
import { getAudioContextClass } from "../utils/audio-context";
import { clamp01 as clampVolume } from "../utils/math";
import { readJsonStorage } from "../utils/storage";
import { persistUiPreferences } from "../utils/ui-preferences";
import useRadioLifecycle from "./hooks/useRadioLifecycle";
import useRadioValue from "./hooks/useRadioValue";
import { DEFAULT_RADIO_SETTINGS, RADIO_STATIONS } from "./radio-config";
import {
  calculateRadioLightingPulse,
  calculateRadioSpectrum,
  isAutoplayBlocked
} from "./radio-utils";

export { RADIO_STATIONS } from "./radio-config";
export { calculateRadioLightingPulse, calculateRadioSpectrum, isAutoplayBlocked } from "./radio-utils";

const STORAGE_KEY = "karaoke-radio";
const STARTUP_FADE_MS = 2000;
const NO_STREAM_ERROR = "No radio stream could be played";
const createVersion = () => Object.create(null);
const RadioContext = createContext(null);

export function normalizeRadioSettings(stored = {}) {
  const stationId = RADIO_STATIONS.some(({ id }) => id === stored.stationId)
    ? stored.stationId
    : DEFAULT_RADIO_SETTINGS.stationId;
  const storedVolume = Number(stored.volume);
  const volume = Number.isFinite(storedVolume)
    ? clampVolume(storedVolume)
    : DEFAULT_RADIO_SETTINGS.volume;
  return {
    stationId,
    volume,
    enabled: typeof stored.enabled === "boolean" ? stored.enabled : DEFAULT_RADIO_SETTINGS.enabled
  };
}
const loadRadioSettings = () => normalizeRadioSettings(readJsonStorage(STORAGE_KEY));

export function RadioProvider({ children }) {
  // An injected literal is stable, so this remains mount-only.
  // Stryker disable next-line ArrayDeclaration
  const initial = useMemo(loadRadioSettings, []);
  const audioRef = useRef(null);
  const audioContextRef = useRef(null);
  const analyserRef = useRef(null);
  const frequencyDataRef = useRef(null);
  const bassRef = useRef(0);
  const lightingBandsRef = useRef(Array(18).fill(0));
  const lightingPulseRef = useRef(0);
  const spectrumRef = useRef(Array(18).fill(0));
  useEffect(
    () =>
      registerLightingSource("radio", () => {
        const active = !!audioRef.current && !audioRef.current.paused && !audioRef.current.ended;
        if (active) {
          lightingPulseRef.current = calculateRadioLightingPulse(
            spectrumRef.current,
            lightingBandsRef.current,
            lightingPulseRef.current
          );
          lightingBandsRef.current = [...spectrumRef.current];
        }
        return { active, level: lightingPulseRef.current, transient: true };
      }),
    []
  );
  const animationRef = useRef(0);
  const volumeFadeRef = useRef(0);
  const analysisVersionRef = useRef(createVersion());
  const streamIndexRef = useRef(0);
  const suspendedRef = useRef(false);
  // Every suspend transition overwrites this before it can be read.
  // Stryker disable next-line BooleanLiteral
  const resumeAfterRecordingRef = useRef(false);
  const playbackVersionRef = useRef(createVersion());
  const streamAttemptRef = useRef(false);
  // Startup state gates gestures until playback writes the real value.
  // Stryker disable next-line BooleanLiteral
  const pendingStartupPlaybackRef = useRef(false);
  const [stationId, setStationIdState] = useState(initial.stationId);
  const [volume, setVolumeState] = useState(initial.volume);
  const [isPlaying, setPlaying] = useState(false);
  const [isLoading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const volumeRef = useRef(initial.volume);
  const station = RADIO_STATIONS.find(({ id }) => id === stationId);
  // Stryker disable ArrayDeclaration: only module functions and stable React refs are captured.
  const persist = useCallback((patch) => {
    persistUiPreferences(api, "radio", { ...loadRadioSettings(), ...patch });
  }, []);
  // Stryker restore ArrayDeclaration
  // Stryker disable ArrayDeclaration: only a stable React ref is captured.
  const cancelVolumeFade = useCallback(() => {
    cancelAnimationFrame(volumeFadeRef.current);
    volumeFadeRef.current = 0;
  }, []);
  // Stryker restore ArrayDeclaration
  // Stryker disable ArrayDeclaration: cancelVolumeFade is stable for the provider lifetime.
  const fadeVolumeIn = useCallback(
    (targetVolume, duration = STARTUP_FADE_MS) => {
      const audio = audioRef.current;
      cancelVolumeFade();
      const target = clampVolume(Number(targetVolume) || 0);
      audio.volume = 0;
      if (target === 0) return;
      const startedAt = performance.now();
      const step = (now) => {
        if (audioRef.current !== audio || audio.paused) {
          volumeFadeRef.current = 0;
          return;
        }
        const progress = Math.min(1, (now - startedAt) / duration);
        // Smoothstep: мягкий старт и мягкое достижение сохранённой громкости.
        const eased = progress * progress * (3 - 2 * progress);
        audio.volume = target * eased;
        volumeFadeRef.current = progress < 1 ? requestAnimationFrame(step) : 0;
      };
      volumeFadeRef.current = requestAnimationFrame(step);
    },
    [cancelVolumeFade]
  );
  // Stryker restore ArrayDeclaration
  // Stryker disable ArrayDeclaration: only stable DOM globals and React refs are captured.
  const stopAnalysis = useCallback(() => {
    analysisVersionRef.current = createVersion();
    cancelAnimationFrame(animationRef.current);
    bassRef.current = 0;
    lightingBandsRef.current.fill(0);
    lightingPulseRef.current = 0;
    spectrumRef.current.fill(0);
    const rootStyle = document.documentElement.style;
    rootStyle.setProperty("--radio-bass", "0");
    rootStyle.setProperty("--radio-analysis-active", "0");
    spectrumRef.current.forEach((_, index) => {
      rootStyle.setProperty(`--radio-band-${index}`, "0");
    });
  }, []);
  // Stryker restore ArrayDeclaration
  // Stryker disable ArrayDeclaration: stopAnalysis is stable for the provider lifetime.
  const startAnalysis = useCallback(() => {
    const analyser = analyserRef.current;
    const meter = frequencyDataRef.current;
    let data;
    const audioContext = audioContextRef.current;
    const analysisVersion = createVersion();
    analysisVersionRef.current = analysisVersion;
    // The only consumer (Library backdrop) samples these custom properties at
    // ~30 Hz via getComputedStyle, and the values it reads are lerped further
    // still -- writing them on every rAF tick (~60 Hz) just doubles style
    // recalculation on documentElement for no visible gain.
    let frameIndex = 0;
    const readBass = () => {
      if (
        analysisVersion !== analysisVersionRef.current ||
        audioContext.state === "closed" ||
        analyserRef.current !== analyser
      )
        return;
      try {
        data = meter.read();
      } catch {
        stopAnalysis();
        return;
      }
      const bands = spectrumRef.current;
      const spectrum = calculateRadioSpectrum(
        data,
        audioContext.sampleRate,
        analyser.fftSize,
        bassRef.current,
        bands
      );
      bassRef.current = spectrum.bass;
      spectrum.bands.forEach((level, index) => {
        bands[index] = level;
      });
      if (frameIndex % 2 === 0) {
        const rootStyle = document.documentElement.style;
        rootStyle.setProperty("--radio-bass", bassRef.current.toFixed(3));
        rootStyle.setProperty("--radio-analysis-active", "1");
        bands.forEach((level, index) => {
          rootStyle.setProperty(`--radio-band-${index}`, level.toFixed(3));
        });
      }
      frameIndex += 1;
      animationRef.current = requestAnimationFrame(readBass);
    };
    cancelAnimationFrame(animationRef.current);
    readBass();
  }, [stopAnalysis]);
  // Stryker restore ArrayDeclaration
  // Stryker disable ArrayDeclaration: only stable React refs are captured.
  const prepareAudioGraph = useCallback(() => {
    const audio = audioRef.current;
    const existing = audioContextRef.current;
    if (existing && existing.state !== "closed") return existing;
    const AudioContext = getAudioContextClass();
    // Absence is also contained by the best-effort caller's catch.
    // Stryker disable next-line ConditionalExpression
    if (!AudioContext) return null;
    const context = new AudioContext();
    const meter = createLevelMeter(context, {
      fftSize: 2048,
      smoothingTimeConstant: 0.72,
      domain: "frequency"
    });
    const { analyser } = meter;
    const source = context.createMediaElementSource(audio);

    source.connect(analyser);
    analyser.connect(context.destination);
    audioContextRef.current = context;
    analyserRef.current = analyser;
    frequencyDataRef.current = meter;
    return context;
  }, []);
  // Stryker restore ArrayDeclaration
  // Stryker disable ArrayDeclaration: both graph callbacks are stable for the provider lifetime.
  const unlockAudioAnalysis = useCallback(async () => {
    try {
      const context = prepareAudioGraph();
      // A missing graph otherwise throws inside this same best-effort catch.
      // Stryker disable next-line ConditionalExpression
      if (!context) return;
      if (context.state === "suspended") await context.resume();
      // The provider audio node exists whenever this callback can run.
      // Stryker disable next-line OptionalChaining
      if (!audioRef.current?.paused) startAnalysis();
    } catch {
      // Радио продолжает играть и без Web Audio. Анализатор подключится
      // после следующего пользовательского жеста.
    }
  }, [prepareAudioGraph, startAnalysis]);
  // Stryker restore ArrayDeclaration
  // Stryker disable ArrayDeclaration: callers always provide the station and refs are stable.
  const loadStream = useCallback((index, nextStation) => {
    const audio = audioRef.current;
    streamIndexRef.current = index;
    audio.src = nextStation.streams[index];
    audio.load();
  }, []);
  // Stryker restore ArrayDeclaration
  const turnOn = useCallback(
    async ({
      remember = true,
      analyse = true,
      startIndex = 0,
      targetStation = station,
      fadeIn = false
    } = {}) => {
      const audio = audioRef.current;
      const playbackVersion = createVersion();
      playbackVersionRef.current = playbackVersion;
      setError("");
      setLoading(true);
      let lastError = null;
      streamAttemptRef.current = true;
      try {
        audio.volume = fadeIn ? 0 : volumeRef.current;
        // The saved Razer mode owns the endpoint exclusively. The existing
        // media-active contract swaps monitoring to shared before audio.play().
        // Radio must keep working when the optional local monitor backend is
        // offline; in that case no backend stream can own the output device.
        await api.setDirectMonitorMediaActive(true).catch(() => {});
        // Try every mirror sequentially. Previously onError switched the stream
        // while the original audio.play() promise was still pending, so that
        // rejected promise could overwrite a successful fallback with an error.
        for (let pass = 0; pass < 2; pass += 1) {
          for (let index = startIndex; index < targetStation.streams.length; index += 1) {
            if (playbackVersion !== playbackVersionRef.current || suspendedRef.current) {
              audio.pause();
              if (suspendedRef.current) await api.setDirectMonitorMediaActive(false);
              return false;
            }
            try {
              loadStream(index, targetStation);
              audio.volume = fadeIn ? 0 : volumeRef.current;
              await audio.play();
              if (playbackVersion !== playbackVersionRef.current || suspendedRef.current) {
                audio.pause();
                if (suspendedRef.current) await api.setDirectMonitorMediaActive(false);
                return false;
              }
              pendingStartupPlaybackRef.current = false;
              setPlaying(true);
              if (fadeIn) fadeVolumeIn(volumeRef.current);
              if (remember) persist({ enabled: true });
              if (analyse) await unlockAudioAnalysis();
              return true;
            } catch (reason) {
              lastError = reason;
              audio.pause();

              // Chromium can still reject autoplay even when Electron requests
              // a permissive policy. This is not a broken stream: defer the
              // startup playback until the first real user gesture instead of
              // cycling mirrors and showing a scary error.
              if (fadeIn && isAutoplayBlocked(reason)) {
                pendingStartupPlaybackRef.current = true;
                setPlaying(false);
                setError("");
                if (playbackVersion === playbackVersionRef.current)
                  await api.setDirectMonitorMediaActive(false);
                return false;
              }
            }
          }

          // A short second pass absorbs transient DNS/network/player startup
          // failures without showing a false error immediately on app launch.
          if (pass === 0) {
            startIndex = 0;
            await new Promise((resolve) => {
              setTimeout(resolve, 500);
            });
          }
        }
        throw lastError || new Error(NO_STREAM_ERROR);
      } catch (reason) {
        if (playbackVersion === playbackVersionRef.current)
          await api.setDirectMonitorMediaActive(false).catch(() => {});
        setPlaying(false);
        setError(
          reason.message
            ? translateSaved("radio.failedToStartRadio", { 0: reason.message })
            : translateSaved("radio.failedToStartRadio2")
        );
        return false;
      } finally {
        streamAttemptRef.current = false;
        if (playbackVersion === playbackVersionRef.current) setLoading(false);
      }
    },
    [fadeVolumeIn, loadStream, persist, station, unlockAudioAnalysis]
  );
  // Stryker disable ArrayDeclaration: all dependencies are stable for the provider lifetime.
  const turnOff = useCallback(
    ({ remember = true } = {}) => {
      playbackVersionRef.current = createVersion();
      cancelVolumeFade();
      audioRef.current.pause();
      api.setDirectMonitorMediaActive(false).catch(() => {});
      setPlaying(false);
      setLoading(false);
      if (remember) persist({ enabled: false });
      stopAnalysis();
    },
    [cancelVolumeFade, persist, stopAnalysis]
  );
  // Stryker restore ArrayDeclaration
  const toggle = useCallback(() => {
    if (isPlaying) turnOff();
    else turnOn();
  }, [isPlaying, turnOff, turnOn]);
  // Stryker disable ArrayDeclaration: persistence and fade cancellation are stable callbacks.
  const setVolume = useCallback(
    (value) => {
      const numericValue = Number(value);
      const next = Number.isFinite(numericValue)
        ? clampVolume(numericValue)
        : DEFAULT_RADIO_SETTINGS.volume;
      volumeRef.current = next;
      setVolumeState(next);
      cancelVolumeFade();
      audioRef.current.volume = next;
      persist({ volume: next });
    },
    [cancelVolumeFade, persist]
  );
  // Stryker restore ArrayDeclaration
  const setStation = useCallback(
    (nextId, { resume } = {}) => {
      const next = RADIO_STATIONS.find(({ id }) => id === nextId);
      if (!next || next.id === stationId) return;
      // Callers that already know the desired end state (e.g. room sync
      // applying a remote {stationId, isPlaying} pair together) can pass
      // `resume` explicitly instead of relying on the current `isPlaying`
      // snapshot -- otherwise a simultaneous station-change + stop signal
      // would resume playback here just before the caller's own turnOff()
      // takes effect, since that state update hasn't committed yet.
      const shouldResume = resume ?? (isPlaying || isLoading);
      playbackVersionRef.current = createVersion();
      // A fade started for the previous station (e.g. the startup fade-in)
      // only cancels itself when a NEW fade begins -- switching stations
      // here doesn't necessarily start one (turnOn below defaults fadeIn to
      // false), so the stale loop would otherwise keep nudging the new
      // station's volume toward the old fade's target for its remaining
      // duration, heard as an unexplained dip-and-recover.
      cancelVolumeFade();
      audioRef.current.pause();
      stopAnalysis();
      setPlaying(false);
      setLoading(false);
      setError("");
      setStationIdState(next.id);
      persist({ stationId: next.id });
      streamIndexRef.current = 0;
      loadStream(0, next);
      if (shouldResume && !suspendedRef.current) turnOn({ remember: false, targetStation: next });
    },
    [cancelVolumeFade, isLoading, isPlaying, loadStream, persist, stationId, stopAnalysis, turnOn]
  );
  const setRecordingActive = useCallback(
    (active) => {
      if (active && !suspendedRef.current) {
        suspendedRef.current = true;
        resumeAfterRecordingRef.current = isPlaying;
        if (isPlaying) turnOff({ remember: false });
        return;
      }
      if (!active && suspendedRef.current) {
        suspendedRef.current = false;
        const shouldResume = resumeAfterRecordingRef.current;
        if (shouldResume) turnOn({ remember: false });
      }
    },
    [isPlaying, turnOff, turnOn]
  );
  const handleStreamError = useCallback(() => {
    // Initial/fallback playback is handled by turnOn() itself so onError must
    // not race with its pending play() promise.
    if (streamAttemptRef.current) return;
    cancelVolumeFade();
    const nextIndex = streamIndexRef.current + 1;
    if (nextIndex < station.streams.length && isPlaying) {
      turnOn({ remember: false, startIndex: nextIndex });
      return;
    }
    setPlaying(false);
    api.setDirectMonitorMediaActive(false).catch(() => {});
    setLoading(false);
    setError(translateSaved("radio.temporarilyUnavailable", { 0: station.name }));
    stopAnalysis();
  }, [cancelVolumeFade, isPlaying, station, stopAnalysis, turnOn]);
  const turnOnRef = useRef(turnOn);
  turnOnRef.current = turnOn;
  useRadioLifecycle({
    analyserRef,
    audioContextRef,
    audioRef,
    cancelVolumeFade,
    createVersion,
    enabled: initial.enabled,
    frequencyDataRef,
    pendingStartupPlaybackRef,
    playbackVersionRef,
    stopAnalysis,
    suspendedRef,
    turnOnRef,
    unlockAudioAnalysis
  });
  const value = useRadioValue({
    bassRef,
    error,
    isLoading,
    isPlaying,
    setRecordingActive,
    setStation,
    setVolume,
    spectrumRef,
    station,
    stationId,
    toggle,
    turnOff,
    turnOn,
    volume
  });
  return (
    <RadioContext.Provider value={value}>
      {children}
      <audio ref={audioRef} crossOrigin="anonymous" preload="none" onError={handleStreamError} />
    </RadioContext.Provider>
  );
}
export function useRadio() {
  const context = useContext(RadioContext);
  if (!context) throw new Error("useRadio must be used inside RadioProvider");
  return context;
}
