# Direct microphone monitoring: control and diagnostics

## Scope

This change concerns the local `audio_service` / `monitor_worker` path. Recording
sessions and online-room WebRTC audio have separate capture/render paths. Passing
these tests does not establish a mic-to-ear or remote-voice latency on Razer hardware.
The native ASIO bridge/start command is unchanged. The split WASAPI output now
fades in its first 5 ms to prevent a startup discontinuity.
`start-dev.bat` does not run the audible acoustic probe on launch. Live monitor
status still prints through the normal console; an acoustic probe must be run
deliberately with the microphone next to the speaker.

## API contract

- `POST /audio/direct-monitor/start` returns **202** with the saved audio settings.
  `monitoring_enabled=true` is desired state, **not proof that audio is running**.
- `POST /audio/direct-monitor/media-active?active=true|false` switches a saved
  exclusive monitor to shared while browser radio plays, then restores the
  selected mode after playback stops.
- `POST /audio/settings` saves desired settings without waiting for the new stream.
  Input names use the last UI device enumeration. Named ASIO selections are checked
  by the existing ASIO start path, not by another bridge invocation inside HTTP.
- `GET /audio/direct-monitor/status` returns `idle`, `starting`, `running`, `stopping`
  or `error`, request ID, selected endpoints, requested/chosen buffer, sample rate,
  driver mode, fallback reason/count and (when available) PortAudio input/output
  latency. These reported endpoint latencies **exclude other DSP/hardware delays**.
- `POST /audio/direct-monitor/stop` still waits for the current child to release
  the device. Recording/room capture can safely follow its response. It cancels
  pending starts without waiting for their device enumeration to finish.
- A startup failure is visible in status; desired settings remain saved so the
  user can correct them and retry, or explicitly turn monitoring off.

The command lane is single-threaded, latest-request-wins, with cancellation checks
before launching a child and while waiting for readiness. Device enumeration is
once per auto-driver configuration; format probes occur in the child. Settings
snapshots, never SQLAlchemy sessions/entities, cross to the hardware thread.
Effect-only edits coalesce into live updates after startup without restarting DSP.

## WASAPI mode

Windows Driver opens shared output. The selectable Razer WASAPI mode opens
exclusive input and output for solo monitoring, using the saved effects.
Radio, karaoke, recording, and room playback switch it to shared before
starting other audio and restore the selected exclusive mode afterward.
The driver's reported buffer latency is not an acoustic
microphone-to-headphone measurement.
On the attached Razer USB Sound Card at 48 kHz, a 96-frame split exclusive
monitor returned 12 consistent coded echoes with a 37.937 ms acoustic median,
zero late underruns, dropped frames, or glitches. A shared monitor returned
24 echoes with an 87.917 ms acoustic median in the latest stable probe (one
glitch). The 2-4 ms API buffer report does not include the full device path.
A later 8-second split-exclusive repeat with the microphone at the speaker
returned 10 consistent monitor echoes at 39.167 ms (39.167-39.188 ms), 19/19
direct speaker pulses at 37.875 ms, and zero late underruns, dropped frames,
or glitches. With all available effects enabled, a 30-second silent-output
stress run had zero late underruns, drops, and glitches; DSP compute was
0.327 ms at the final sample and octave reported 6 ms algorithmic delay.
These are separate diagnostics and do not demonstrate <=15 ms monitoring with
effects.
Exclusive output prevents an unrelated application or website from playing
through the same Razer endpoint. A native exclusive-input/shared-output probe
retained other playback but reported 45.468 ms internal stream latency with
roughly 38 ms output clock lead; it is not a low-latency replacement. A
prototype mixing a virtual-cable input in Python lost 50,784 of 240,384
render frames in five seconds, so it was not integrated.
A native exclusive-render experiment negotiated 144 frames (3 ms) on both
Razer endpoints and displayed about 7 ms internal stream latency, but in
8 seconds dropped 243,888 of 383,184 captured frames with 253 queue
underruns. It was rejected and removed from product code. The installed
Razer endpoint uses the THX Ltd. USB Audio extension (oem79.inf) on the
Windows USB Audio service, with THX APO components. Exclusive WASAPI already
bypasses the shared audio-engine APO path; THX Spatial was already disabled
during these measurements. Synapse sidetone was heard by the
user, but the quiet acoustic test detected only one possible 8.167 ms control
echo; that is insufficient to establish stable hardware sidetone latency and
sidetone does not run the application's effects.
Candidate formats prefer native endpoint rates. Fallback never deliberately
selects a smaller explicit buffer after sustained callback glitches.
Automatic periodic return to a smaller buffer is intentionally omitted: a
manual buffer choice/retry avoids unrequested interruptions while singing.

## Verification and remaining hardware check

Automated checks cover one enumeration, fast HTTP acknowledgement with blocked
hardware (approximately 9 ms in the local TestClient check, target <200 ms), latest
request cancellation, stop during enumeration, stale events, live effects updates,
exclusive/shared candidates, measured-versus-requested latency, and UI actions.

Before claiming a real Razer improvement, test on that user's card:

1. Record selected endpoint/mode/buffer and actual startup time from status.
2. Compare shared and explicitly selected exclusive capture with buffer 128.
3. Verify backing-track playback, recording start/stop and room microphone handover.
4. Run under normal desktop load; observe glitch fallback counters.
5. Measure physical loopback mic-to-output latency before/after. A successful HTTP
   test or a small requested buffer alone is not this measurement.

The latest profile run passed 209 backend tests, 85 frontend tests, and the
production frontend build. The full
frontend suite still has pre-existing Library failures (missing results module,
search-field accessible name, virtualizer's `<60` assertion). These are unrelated
to monitoring and were not changed in this task. No installer was rebuilt or
installed.
