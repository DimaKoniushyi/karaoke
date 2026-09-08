# Direct microphone monitoring: control and diagnostics

## Scope

This change concerns the local `audio_service` / `monitor_worker` path. Recording
sessions and online-room WebRTC audio have separate capture/render paths. Passing
these tests does not establish a mic-to-ear or remote-voice latency on Razer hardware.
The DSP implementations and native ASIO bridge/start command are unchanged.

## API contract

- `POST /audio/direct-monitor/start` returns **202** with the saved audio settings.
  `monitoring_enabled=true` is desired state, **not proof that audio is running**.
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

Monitoring always opens the device in shared mode, on every path (solo self-
monitoring, recording, and the online room) -- exclusive mode is not offered
and is not planned; it seizes the device from every other app (and every
other stream in this app), which this project deliberately never asks for.
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

Production frontend build, 108 focused backend tests, 65 focused frontend tests,
and lint on changed implementation files pass. The full
frontend suite still has pre-existing Library failures (missing results module,
search-field accessible name, virtualizer's `<60` assertion). These are unrelated
to monitoring and were not changed in this task. No installer was rebuilt or
installed, and no consumer USB audio hardware measurement was performed.
