import contextlib
import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import config
import models
from AI.utils.numeric import clamp01
from app.services.audio_relay import AudioRelayServer
from app.services.audio_runtime import hardware_lock, run_on_audio_thread
from app.services.db_utils import commit_refresh
from app.services.monitor_control import MonitorCancelled, MonitorControl

# PortAudio's bundled binary only exposes the real "ASIO" host API when this
# is set before sounddevice loads its native library (sounddevice.py checks
# it once at import time to pick libportaudio64bit-asio.dll over the plain
# build). Without it, no device ever reports host API "ASIO" -- every ASIO
# driver match below silently falls back to whichever MME/WASAPI/DirectSound
# device happens to share a name with it, instead of the real interface.
os.environ.setdefault("SD_ENABLE_ASIO", "1")

try:
    import numpy as np
    import sounddevice as sd

    _AUDIO_BACKEND_AVAILABLE = True
except Exception:
    from types import SimpleNamespace

    sd = SimpleNamespace(
        query_devices=None,
        query_hostapis=None,
        rec=None,
        wait=None,
        default=SimpleNamespace(device=(None, None)),
        WasapiSettings=None,
    )
    _AUDIO_BACKEND_AVAILABLE = False


_monitor_process: subprocess.Popen[str] | None = None
_monitor_reader: threading.Thread | None = None
# Owns the loopback socket monitor_worker.py's RelayLink connects to, and
# fans processed monitor audio out to WebSocket subscribers (see
# app.routers.audio_relay). Lives and dies with _monitor_process -- see
# _start_shared_monitor / _stop_monitoring_process.
_monitor_relay: AudioRelayServer | None = None
# Sticky across live-setting-triggered reconfigures (see configure_monitoring's
# relay_needed=None case) -- only an explicit True/False caller (room-relay
# start, or _restore_monitoring on the way out) ever changes this.
_monitor_relay_needed = False
_monitor_lock = threading.Lock()
_EMPTY_MONITOR_SIGNAL = {"rms_db": -120.0, "clipping": False, "silent": True}
_MONITOR_RESTART_FIELDS = frozenset(
    {
        "input_device_id",
        "output_device_id",
        "audio_driver",
        "asio_driver_name",
        "buffer_size",
    }
)
# The out-of-process worker for the "auto" driver takes volume/reverb/echo/
# delay updates live over stdin (see _send_live_update) instead of a full
# stream restart. The ASIO bridge is a separate native binary with no
# live-update channel, so it still needs a restart to pick up new values.
_ASIO_ONLY_RESTART_FIELDS = frozenset(
    {"volume", "reverb", "echo", "delay", "noise_suppression", "octave"}
)
_LIVE_UPDATE_FIELDS = frozenset({"volume", "reverb", "echo", "delay", "noise_suppression", "octave"})
# MME (and any other non-WASAPI host, e.g. DirectSound) cannot reliably
# service the tiny per-callback buffers WASAPI/ASIO can. Unlike ASIO/native
# WASAPI, which reject or clamp an out-of-range buffer up front, this plain
# PortAudio path just opens the stream and glitches continuously -- heard as
# static instead of voice, not a startup error. Floor the requested buffer
# for that path instead of silently producing garbage audio.
_PLAIN_HOST_MIN_BLOCKSIZE = 512
_monitor_signal = dict(_EMPTY_MONITOR_SIGNAL)
_monitor_effects_disabled = False


def settings_snapshot(settings, **overrides):
    """A detached copy of the monitor-relevant settings fields.

    Used instead of passing a live, DB-session-bound AudioSettings row
    around: that session can be long closed by the time a deferred/async
    caller (a coalesced monitor restart, a request-scoped override) actually
    reads it, and mutating the row in place to apply a transient override
    risks a concurrent reader observing the transient values.
    """
    fields = {
        field: getattr(settings, field, None)
        for field in _MONITOR_RESTART_FIELDS | _LIVE_UPDATE_FIELDS | {"monitoring_enabled"}
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)
_monitor_dry_bypass = False
# See check_signal_quality: throttles the ad-hoc sd.rec() probe it falls
# back to whenever nothing is actively monitoring/recording yet.
_SIGNAL_PROBE_INTERVAL_SEC = 1.5
_signal_probe_cache: dict = {"at": 0.0, "device": object(), "gain": None, "result": None}
_MONITOR_START_TIMEOUT_SECONDS = 12.0
logger = logging.getLogger(__name__)
_monitor_control = MonitorControl(execution_lock=hardware_lock)
_requested_effects_disabled = False
_hardware_suspended = False
_known_device_names: dict[int, str] = {}


def _asio_bridge_path() -> Path:
    return (
        Path(sys.executable).with_name("KaraokeAsioBridge.exe")
        if config.IS_FROZEN
        else Path(config.PROJECT_ROOT) / "generated" / "build" / "asio" / "KaraokeAsioBridge.exe"
    )


def list_asio_drivers() -> list[str]:
    bridge = _asio_bridge_path()
    if not bridge.is_file():
        return []
    try:
        result = subprocess.run(
            [str(bridge), "--list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        drivers = payload.get("drivers", [])
        return [str(name) for name in drivers if isinstance(name, str)]
    except (OSError, ValueError, subprocess.SubprocessError, IndexError) as exc:
        logger.warning("Could not enumerate ASIO drivers: %s", exc)
        return []


def _device_tokens(name: str) -> set[str]:
    return {
        token
        for token in re.findall("[\\w]+", name.casefold())
        if len(token) > 2 and token not in {"audio", "device", "микрофон", "наушники"}
    }


def _device_latency(device: dict, kind: str) -> float:
    value = device.get(f"default_low_{kind}_latency", 1.0)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 1.0


def _low_latency_equivalent(
    device_id: int | None,
    kind: str,
    devices=None,
    *,
    preferred_host_api: str = "wasapi",
    preferred_name: str | None = None,
) -> int:
    devices = sd.query_devices() if devices is None else devices
    source_id = _resolved_device_index(device_id, kind, devices, preferred_name=preferred_name)
    source, capability = devices[source_id], f"max_{kind}_channels"
    source_name = str(source.get("name", "")).casefold().strip()
    source_tokens = _device_tokens(str(source.get("name", "")))
    best: tuple[float, int] | None = None
    for index, candidate in enumerate(devices):
        if int(candidate.get(capability, 0)) < 1:
            continue
        host = _host_api_name(candidate).casefold()
        if preferred_host_api not in host:
            continue
        overlap = len(source_tokens & _device_tokens(str(candidate.get("name", ""))))
        if device_id is not None and overlap == 0 and index != source_id:
            continue
        score = 300 + overlap * 30 - _device_latency(candidate, kind) * 1000
        if str(candidate.get("name", "")).casefold().strip() == source_name:
            score += 500
        if index == source_id:
            score += 20
        if best is None or score > best[0]:
            best = (score, index)
    return best[1] if best else source_id


def _matching_output_for_input(
    input_id: int, output_id: int | None, devices=None, *, preferred_name: str | None = None
) -> int:
    devices = sd.query_devices() if devices is None else devices
    selected_output = _low_latency_equivalent(output_id, "output", devices, preferred_name=preferred_name)
    input_info = devices[input_id]
    input_host_api = int(input_info["hostapi"])
    if output_id is not None:
        output_info = devices[selected_output]
        if int(output_info["hostapi"]) == input_host_api:
            return selected_output
        selected_output = _resolved_device_index(output_id, "output", devices, preferred_name=preferred_name)
        if int(devices[selected_output]["hostapi"]) == input_host_api:
            return selected_output
    input_name = str(input_info.get("name", "")).casefold().strip()
    input_tokens, input_rate = (
        _device_tokens(str(input_info.get("name", ""))),
        float(input_info.get("default_samplerate", 0) or 0),
    )
    candidates: list[tuple[float, int]] = []
    for index, candidate in enumerate(devices):
        if int(candidate.get("max_output_channels", 0)) < 1:
            continue
        if int(candidate["hostapi"]) != input_host_api:
            continue
        host = _host_api_name(candidate).casefold()
        overlap = len(input_tokens & _device_tokens(str(candidate.get("name", ""))))
        rate = float(candidate.get("default_samplerate", 0) or 0)
        score = float((300 if "wasapi" in host else 100) + overlap * 35)
        if str(candidate.get("name", "")).casefold().strip() == input_name:
            score += 500
        score -= abs(rate - input_rate) / 1000
        score -= _device_latency(candidate, "output") * 1000
        if index == selected_output:
            score += 45
        candidates.append((score, index))
    return max(candidates)[1] if candidates else selected_output


def _asio_device_hint(driver_name: str | None) -> str:
    if not driver_name:
        return ""
    hint = driver_name.lower()
    for suffix in (" asio driver", " asio", " driver"):
        hint = hint.replace(suffix, "")
    return " ".join(hint.split())


def _matching_asio_device_index(driver_name: str | None, kind: str) -> int | None:
    if not _AUDIO_BACKEND_AVAILABLE:
        return None
    hint = _asio_device_hint(driver_name)
    if not hint:
        return None
    capability = "max_input_channels" if kind == "input" else "max_output_channels"
    best: tuple[int, int] | None = None
    for index, device in enumerate(sd.query_devices()):
        if int(device.get(capability, 0)) < 1:
            continue
        # Unlike _resolved_device_index's plain-driver path, this scores by
        # raw name-token overlap across every host API PortAudio exposes for
        # the interface (ASIO, WASAPI, MME, DirectSound, WDM-KS all share the
        # same product name) -- without this exclusion a same-named WDM-KS
        # entry could legitimately out-score the real ASIO one and get
        # returned instead, and that pin can fail outright just being opened
        # (see _is_wdm_ks_device).
        if _is_wdm_ks_device(device):
            continue
        name = str(device.get("name", "")).lower()
        overlap = sum(token in name for token in hint.split() if len(token) > 2)
        if overlap == 0:
            continue
        score = overlap * 10 + (5 if _is_asio_device(device) else 0)
        if best is None or score > best[0]:
            best = (score, index)
    return best[1] if best else None


def device_snapshot() -> list[dict] | None:
    """One PortAudio device-table enumeration, for callers that resolve
    several preferred_*_device()/preferred_sample_rate() values from the same
    settings and would otherwise each re-enumerate independently.
    """
    return sd.query_devices() if _AUDIO_BACKEND_AVAILABLE else None


def preferred_input_device(
    device_id: int | None,
    driver: str = "auto",
    asio_driver_name: str | None = None,
    devices=None,
    *,
    device_name: str | None = None,
) -> int | None:
    if driver == "asio":
        available = devices if devices is not None else (sd.query_devices() if _AUDIO_BACKEND_AVAILABLE else [])
        if (
            device_id is not None and _AUDIO_BACKEND_AVAILABLE
            and 0 <= device_id < len(available)
            and (_is_wdm_ks_device(available[device_id]) or not _is_asio_device(available[device_id]))
        ):
            # Saved id now resolves to a broken WDM-KS pin, or (most commonly)
            # to a plain WASAPI/MME device left over from a previous "auto"
            # driver selection -- returning it here would make PortAudio-based
            # recording open a completely different physical/logical endpoint
            # than the one the native ASIO bridge is actually monitoring
            # through. Re-match by name instead.
            device_id = None
        if device_id is not None and (not _AUDIO_BACKEND_AVAILABLE or 0 <= device_id < len(available)):
            return device_id
        return _matching_asio_device_index(asio_driver_name, "input")
    if not _AUDIO_BACKEND_AVAILABLE:
        return device_id
    # "mme" pins the search to an MME-hosted equivalent instead of the
    # default "auto" preference for a WASAPI one -- otherwise an explicitly
    # selected MME device would be silently upgraded back to a same-named
    # WASAPI device, making it impossible to actually monitor (and compare
    # latency) on plain MME.
    return _low_latency_equivalent(
        device_id,
        "input",
        devices,
        preferred_host_api="mme" if driver == "mme" else "wasapi",
        preferred_name=device_name,
    )


def _host_api_name(device: dict) -> str:
    return str(sd.query_hostapis(device["hostapi"])["name"])


def _is_asio_device(device: dict) -> bool:
    return "asio" in _host_api_name(device).lower()


def _is_wasapi_device(device: dict) -> bool:
    return "wasapi" in _host_api_name(device).casefold()


def _is_wdm_ks_device(device: dict) -> bool:
    # PortAudio enumerates a "Windows WDM-KS" host API entry for most
    # capture devices, but nothing in this app opens a stream against it
    # deliberately (it is filtered out of every device picker) and it was
    # never wired up or tested -- on some devices/drivers its pin doesn't
    # support a basic property query PortAudio needs just to open a stream
    # ("Unanticipated host error" / WdmSyncIoctl DeviceIoControl failure on
    # KSPROPERTY_PIN_PHYSICALCONNECTION), which aborts the stream outright.
    # A saved or auto-resolved device id must never be allowed to land on
    # this host API, only fall through to one the app actually supports.
    return "wdm-ks" in _host_api_name(device).casefold()


def _resolved_device_index(
    device_id: int | None, kind: str, devices=None, *, preferred_name: str | None = None
) -> int:
    devices = sd.query_devices() if devices is None else devices
    if (
        device_id is not None and 0 <= device_id < len(devices)
        and not _is_wdm_ks_device(devices[device_id])
    ):
        return device_id
    # The saved index no longer resolves -- most commonly a USB interface
    # that got re-enumerated at a different index after being unplugged and
    # reconnected (or the app restarted after Windows renumbered devices).
    # Re-find it by its saved name before falling through to the system
    # default, so a USB card the user picked keeps being used across
    # reconnects instead of silently reverting to onboard audio.
    if preferred_name:
        target = preferred_name.casefold().strip()
        capability = f"max_{kind}_channels"
        named_match = next(
            (
                index
                for index, device in enumerate(devices)
                if int(device.get(capability, 0)) > 0
                and not _is_wdm_ks_device(device)
                and str(device.get("name", "")).casefold().strip() == target
            ),
            None,
        )
        if named_match is not None:
            return named_match
    default_input, default_output = sd.default.device
    raw_default = default_input if kind == "input" else default_output
    try:
        default_id = int(raw_default)
    except (TypeError, ValueError):
        default_id = -1
    if (
        0 <= default_id < len(devices)
        and int(devices[default_id].get(f"max_{kind}_channels", 0)) > 0
        and not _is_wdm_ks_device(devices[default_id])
    ):
        return default_id
    capability = f"max_{kind}_channels"
    candidates = [
        (index, device)
        for index, device in enumerate(devices)
        if int(device.get(capability, 0)) > 0 and not _is_wdm_ks_device(device)
    ]
    if not candidates:
        raise RuntimeError(f"No {kind} audio device is available")
    host_priority = {"wasapi": 0, "mme": 1, "directsound": 2}

    def rank(item: tuple[int, dict]) -> tuple[int, float]:
        _index, device = item
        host = _host_api_name(device).casefold().replace("windows ", "")
        priority = next((value for name, value in host_priority.items() if name in host), 4)
        return priority, _device_latency(device, kind)

    return min(candidates, key=rank)[0]


def preferred_output_device(
    input_device_id: int | None = None,
    driver: str = "auto",
    output_device_id: int | None = None,
    asio_driver_name: str | None = None,
    devices=None,
    *,
    device_name: str | None = None,
) -> int | None:
    if not _AUDIO_BACKEND_AVAILABLE:
        return output_device_id
    devices = sd.query_devices() if devices is None else devices
    if driver == "asio":
        if output_device_id is not None:
            # Same mismatch as preferred_input_device: a saved output id left
            # over from a non-ASIO driver selection must not be handed to
            # PortAudio recording as if it were the interface the native ASIO
            # bridge is actually monitoring through.
            if 0 <= output_device_id < len(devices) and _is_asio_device(devices[output_device_id]):
                return output_device_id
            output_device_id = None
        if input_device_id is not None and 0 <= input_device_id < len(devices):
            device = devices[input_device_id]
            if _is_asio_device(device) and int(device.get("max_output_channels", 0)) > 0:
                return input_device_id
        return _matching_asio_device_index(asio_driver_name, "output")
    resolved_input = _low_latency_equivalent(
        input_device_id, "input", devices, preferred_host_api="mme" if driver == "mme" else "wasapi"
    )
    return _matching_output_for_input(
        resolved_input, output_device_id, devices, preferred_name=device_name
    )


def preferred_sample_rate(input_device_id: int | None = None, driver: str = "auto", devices=None) -> int:
    if driver == "asio":
        # PortAudio's device table is captured once (first ASIO host-API
        # query in this process) and never refreshed, but the ASIO bridge can
        # change the interface's actual clock at monitor start (see
        # _start_asio_monitor). When the bridge is running, trust the rate it
        # reported back after negotiating with the driver instead of that
        # stale snapshot -- otherwise the WAV file gets stamped with a rate
        # the hardware isn't actually running at, and playback speeds up or
        # slows down.
        monitor = _monitor_control.snapshot()
        if monitor.get("mode") == "ASIO" and monitor.get("state") == "running":
            live_rate = monitor.get("sample_rate")
            if isinstance(live_rate, (int, float)) and live_rate > 0:
                return int(round(live_rate))
    if _AUDIO_BACKEND_AVAILABLE and input_device_id is not None:
        devices = sd.query_devices() if devices is None else devices
        if 0 <= input_device_id < len(devices):
            return int(round(float(devices[input_device_id]["default_samplerate"])))
    return config.RECORDING_SAMPLE_RATE


def _monitor_sample_rate(input_device_id: int, output_device_id: int, devices=None) -> float:
    input_info = sd.query_devices(input_device_id) if devices is None else devices[input_device_id]
    output_info = sd.query_devices(output_device_id) if devices is None else devices[output_device_id]
    input_default = int(round(float(input_info.get("default_samplerate", 0) or 0)))
    output_default = int(round(float(output_info.get("default_samplerate", 0) or 0)))
    # Staying at the endpoints' shared native rate avoids an additional
    # Windows resampler and its buffer. Prefer a common native rate; when the
    # endpoints differ, the render endpoint's mix format is the next best
    # choice, followed by the capture format and standard fallbacks.
    common_default = input_default if input_default == output_default else 0
    candidates = dict.fromkeys(
        (common_default, output_default, input_default, 48_000, 44_100)
    )
    if devices is not None:
        # check_*_settings internally queries devices again. Let the isolated
        # worker open/probe formats; the parent uses exactly one enumeration.
        return float(next(rate for rate in candidates if rate > 0))
    check_input = getattr(sd, "check_input_settings", None)
    check_output = getattr(sd, "check_output_settings", None)
    if callable(check_input) and callable(check_output):
        for sample_rate in candidates:
            if sample_rate <= 0:
                continue
            try:
                check_input(device=input_device_id, channels=1, samplerate=sample_rate)
                check_output(device=output_device_id, channels=1, samplerate=sample_rate)
                return float(sample_rate)
            except Exception:  # PortAudio exposes backend-specific format errors.
                continue
    return float(input_default or output_default or config.RECORDING_SAMPLE_RATE)


def _list_devices(kind: str) -> list[dict]:
    global _known_device_names
    if not _AUDIO_BACKEND_AVAILABLE:
        return []
    channel_field, result = f"max_{kind}_channels", []
    devices = sd.query_devices()
    _known_device_names = {index: str(device.get("name") or "") for index, device in enumerate(devices)}
    for index, device in enumerate(devices):
        if device.get(channel_field, 0) <= 0:
            continue
        host_api = _host_api_name(device)
        result.append(
            {
                "index": index,
                "name": f"{device.get('name', f'device-{index}')} [{host_api}]",
                channel_field: device.get(channel_field, 0),
                "default_samplerate": device.get("default_samplerate"),
                "host_api": host_api,
                "is_asio": "asio" in host_api.lower(),
            }
        )
    return result


def list_input_devices() -> list[dict]:
    return _list_devices("input")


def list_output_devices() -> list[dict]:
    return _list_devices("output")


def _get_or_create_settings(db: Session) -> models.AudioSettings:
    if (settings := db.get(models.AudioSettings, 1)) is not None:
        return settings
    settings = models.AudioSettings(id=1)
    db.add(settings)
    try:
        return commit_refresh(db, settings)
    except IntegrityError:
        # Two API requests can observe the singleton row as missing at the
        # same time.  The winner creates id=1; the loser must reuse it rather
        # than turning a harmless first-run race into HTTP 500.
        if (settings := db.get(models.AudioSettings, 1)) is not None:
            return settings
        raise


def get_settings(db: Session) -> models.AudioSettings:
    return _get_or_create_settings(db)


def _device_name_from(devices: list[dict], device_id: int | None) -> str | None:
    if device_id is None or not (0 <= device_id < len(devices)):
        return None
    return str(devices[device_id].get("name") or "") or None


def _input_device_name(device_id: int | None) -> str | None:
    if device_id is None or not _AUDIO_BACKEND_AVAILABLE:
        return None
    return _device_name_from(sd.query_devices(), device_id)


# Mirrors _input_device_name -- output_device_id is just as much a
# PortAudio index (not a stable identity) as input_device_id, but had no
# saved name at all to recover it by after a USB reconnect/reorder.
_output_device_name = _input_device_name


def _normalized_settings_patch(
    settings: models.AudioSettings, patch: dict, *, resolve_devices: bool = True
) -> tuple[dict, set[str]]:
    updates: dict = {}
    changed_fields: set[str] = set()
    # Resolved once (not per-field): a single patch can change both
    # input_device_id and output_device_id, and each used to re-enumerate the
    # full PortAudio device table independently.
    needs_devices = resolve_devices and _AUDIO_BACKEND_AVAILABLE and any(
        patch.get(field) is not None for field in ("input_device_id", "output_device_id")
    )
    devices = sd.query_devices() if needs_devices else None
    for field, value in patch.items():
        if field in {"input_device_id", "output_device_id"} and value is None:
            if getattr(settings, field) is not None:
                updates[field] = None
                changed_fields.add(field)
            if field == "input_device_id":
                updates["input_device_name"] = None
            elif field == "output_device_id":
                updates["output_device_name"] = None
            continue
        if value is None:
            continue
        if getattr(settings, field) != value:
            updates[field] = value
            changed_fields.add(field)
        if field in {"input_device_id", "output_device_id"}:
            name_field = "input_device_name" if field == "input_device_id" else "output_device_name"
            updates[name_field] = (
                (_device_name_from(devices, value) if devices is not None else None) if resolve_devices
                else (_known_device_names.get(value) or None)
            )

    driver, asio_name = (
        updates.get("audio_driver", settings.audio_driver),
        updates.get("asio_driver_name", settings.asio_driver_name),
    )
    if driver not in {"auto", "asio", "mme"}:
        raise RuntimeError("Unsupported audio driver")
    if driver == "asio" and {"audio_driver", "asio_driver_name"} & changed_fields:
        if not resolve_devices:
            # A named ASIO selection is validated by the unchanged native start
            # path in the background. Do not run a second bridge --list process
            # in the settings request (it has a four-second hardware timeout).
            if not asio_name:
                raise RuntimeError("Select an ASIO driver before enabling ASIO")
            return updates, changed_fields
        drivers = list_asio_drivers()
        if not drivers:
            raise RuntimeError("Native ASIO bridge is not installed or no ASIO drivers were found")
        if asio_name not in drivers:
            updates["asio_driver_name"] = drivers[0]
            changed_fields.add("asio_driver_name")
    return updates, changed_fields


def update_settings(db: Session, patch: dict, *, background: bool = False) -> models.AudioSettings:
    settings = _get_or_create_settings(db)
    updates, changed_fields = _normalized_settings_patch(settings, patch, resolve_devices=not background)
    from app.services import recording_service
    if changed_fields & {"input_device_id", "output_device_id", "audio_driver", "asio_driver_name", "buffer_size"} and recording_service.has_live_capture():
        raise RuntimeError("Stop recording before changing audio devices, driver or buffer")
    previous, driver = (
        {field: getattr(settings, field) for field in updates},
        updates.get("audio_driver", settings.audio_driver),
    )
    restart_fields = _MONITOR_RESTART_FIELDS | (
        _ASIO_ONLY_RESTART_FIELDS if driver == "asio" else set()
    )
    reconfigure_monitoring, live_update_fields = (
        bool(
            "monitoring_enabled" in changed_fields
            or (settings.monitoring_enabled and restart_fields & changed_fields)
        ),
        set() if driver == "asio" else _LIVE_UPDATE_FIELDS & changed_fields,
    )

    for field, value in updates.items():
        setattr(settings, field, value)

    if background:
        # Persist desired settings before handing a plain snapshot to the hardware lane.
        # Hardware failures are reported by /direct-monitor/status, not as a false
        # promise that an accepted settings write has already opened the device.
        commit_refresh(db, settings)
        recording_service.update_capture_controls(updates)
        if reconfigure_monitoring:
            request_monitoring(settings)
        elif live_update_fields and settings.monitoring_enabled:
            payload = {field: getattr(settings, field) for field in _LIVE_UPDATE_FIELDS}
            _monitor_control.update_live(lambda: _send_live_update(payload))
        return settings

    try:
        if reconfigure_monitoring:
            configure_monitoring(settings)
        elif live_update_fields and settings.monitoring_enabled:
            _send_live_update({field: getattr(settings, field) for field in live_update_fields})
        db.commit()
        db.refresh(settings)
        recording_service.update_capture_controls(updates)
        return settings
    except Exception:
        db.rollback()
        for field, value in previous.items():
            setattr(settings, field, value)
        if reconfigure_monitoring:
            try:
                configure_monitoring(settings)
            except Exception as restore_error:
                logger.warning(
                    "Could not restore direct monitoring after settings failure: %s",
                    restore_error,
                )
        raise


def set_monitoring_enabled(
    db: Session, enabled: bool, *, disabled_effects: bool = False,
    background: bool = False,
) -> models.AudioSettings:
    global _monitor_effects_disabled
    settings = get_settings(db)
    previous = settings.monitoring_enabled
    previous_effect_mode = _monitor_effects_disabled
    if background:
        settings.monitoring_enabled = enabled
        commit_refresh(db, settings)
        request_monitoring(settings, disabled_effects=disabled_effects)
        return settings
    _monitor_effects_disabled = bool(disabled_effects) if enabled else False
    if previous == enabled:
        if enabled:
            configure_monitoring(settings)
        else:
            stop_monitoring()
        return settings

    settings.monitoring_enabled = enabled
    try:
        if enabled:
            configure_monitoring(settings)
        else:
            stop_monitoring()
        return commit_refresh(db, settings)
    except Exception:
        db.rollback()
        settings.monitoring_enabled = previous
        _monitor_effects_disabled = previous_effect_mode
        try:
            configure_monitoring(settings)
        except Exception as restore_error:
            logger.warning("Could not restore direct monitoring after failure: %s", restore_error)
        raise


def request_monitoring(
    settings, *, disabled_effects=None, adopt_driver_buffer: bool = False
) -> None:
    global _requested_effects_disabled
    if _hardware_suspended:
        _monitor_control.cancel()
        return
    snapshot = settings_snapshot(settings)
    if disabled_effects is not None:
        _requested_effects_disabled = bool(disabled_effects)
    effects_disabled = _requested_effects_disabled

    def apply():
        global _monitor_effects_disabled
        _monitor_effects_disabled = effects_disabled if snapshot.monitoring_enabled else False
        configure_monitoring(snapshot, adopt_driver_buffer=adopt_driver_buffer)

    _monitor_control.submit(
        apply, state="starting" if snapshot.monitoring_enabled else "stopping",
        requested_blocksize=snapshot.buffer_size, driver=snapshot.audio_driver,
    )


def monitoring_status() -> dict:
    return _monitor_control.snapshot()


def recording_monitor_mode(device_id):
    if _AUDIO_BACKEND_AVAILABLE and _is_wasapi_device(sd.query_devices(device_id)):
        return "shared"
    return "plain"


def stop_monitoring() -> None:
    # Recording and shutdown must invalidate even a request still enumerating
    # devices, so it cannot resurrect the monitor after recording takes ownership.
    _monitor_control.cancel()
    from app.services import recording_service
    with hardware_lock:
        recording_service.update_capture_controls({"monitoring_enabled": False})
        _stop_monitoring_process()


def suspend_monitoring() -> None:
    """Release the microphone while preserving the user's saved preference."""
    global _hardware_suspended
    _hardware_suspended = True
    _monitor_control.cancel()
    from app.services import recording_service
    with hardware_lock:
        recording_service.update_capture_controls({"monitoring_enabled": False})
        _stop_monitoring_process()


def resume_monitoring(settings) -> None:
    """Reapply persisted monitoring after the desktop window is restored."""
    global _hardware_suspended
    _hardware_suspended = False
    if settings.monitoring_enabled:
        request_monitoring(settings)


def _stop_monitoring_process(expected_process=None) -> None:
    global _monitor_process, _monitor_reader, _monitor_relay
    with _monitor_lock:
        if expected_process is not None and _monitor_process is not expected_process:
            return
        process = _monitor_process
        reader = _monitor_reader
        relay = _monitor_relay
        _monitor_process = None
        _monitor_reader = None
        _monitor_relay = None
        _monitor_signal.update(_EMPTY_MONITOR_SIGNAL)
    if relay is not None:
        relay.close()
    if process is None:
        return
    try:
        # A process that already exited on its own (driver crash, hardware
        # unplugged) needs no terminate/kill -- but the pipes below still
        # need closing and the reader thread still needs joining either way;
        # returning early here used to skip that whole cleanup, leaking
        # process.stdout/stdin until the next stop happened to catch a still
        # -running process.
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.5)
            except OSError as exc:
                logger.warning("Could not stop direct monitoring worker: %s", exc)
    finally:
        # Join the reader before closing stdout: it is still iterating
        # `for line in process.stdout`, and closing the file out from under
        # that loop on another thread can raise inside the reader thread.
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if process.stdout is not None:
            process.stdout.close()
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()


def subscribe_monitor_relay() -> tuple[AudioRelayServer, queue.Queue] | None:
    """Subscribes to the currently active Python monitor's relay of
    processed dry/wet audio, or returns None if no relay is running right
    now (monitoring is off, or the active driver is ASIO -- see
    _open_monitor_relay, which only the WASAPI worker path opens).

    Used by the WebSocket route in app.routers.audio_relay. Callers must
    hold onto the returned AudioRelayServer and call
    relay.unsubscribe(subscriber) on it directly during cleanup, rather than
    re-resolving "the current relay" later -- the monitor (and its relay)
    can be restarted mid-subscription by an unrelated settings change.
    """
    with _monitor_lock:
        relay = _monitor_relay
    # 8 slots at RelayLink's ~5ms chunking is ~40ms of backlog before the
    # server starts dropping the oldest queued frame -- live duet singing
    # should lose a little audio to a brief stall rather than build up a
    # queue of old voice waiting to play (the previous default of 32 could
    # let backlog reach several hundred ms for dry+wet before that kicked in).
    return (relay, relay.subscribe(maxsize=8)) if relay is not None else None


def _send_live_update(payload: dict) -> None:
    from app.services import recording_service
    recording_service.update_capture_controls(payload)
    if _monitor_effects_disabled:
        payload = {
            key: (0.0 if key in {"reverb", "echo", "delay", "octave", "noise_suppression"} else value)
            for key, value in payload.items()
        }
    with _monitor_lock:
        process = _monitor_process
    if process is None or process.poll() is not None or process.stdin is None:
        return
    try:
        process.stdin.write(json.dumps(payload) + "\n")
        process.stdin.flush()
    except (OSError, ValueError) as exc:
        logger.warning("Could not push live update to audio monitor worker: %s", exc)


def set_monitor_dry_bypass(db: Session, enabled: bool) -> dict:
    """A momentary "listen to the raw voice" toggle: bypasses the whole
    gate/compressor/tone-shaping/effects chain for local monitoring, without
    touching any saved setting. Only the Python-driven engines (WASAPI
    monitor_worker.py, and the in-process recording-session callback) have a
    live-update channel for this; the native ASIO bridge has none (see
    _MONITOR_RESTART_FIELDS) and does not implement a dry bypass at all, so
    this is a deliberate no-op there rather than a restart with no audible
    effect.
    """
    global _monitor_dry_bypass
    _monitor_dry_bypass = bool(enabled)
    settings = get_settings(db)
    supported = settings.audio_driver != "asio"
    if supported and settings.monitoring_enabled:
        _send_live_update({"dry_monitor": 1.0 if _monitor_dry_bypass else 0.0})
    return {"dry_monitor": _monitor_dry_bypass, "supported": supported}


def configure_monitoring(
    settings: models.AudioSettings, *, adopt_driver_buffer: bool = False, relay_needed: bool | None = None
) -> None:
    global _monitor_relay_needed
    # None means "leave it as whatever the last explicit caller asked for" --
    # settings changes that arrive mid-room-session (e.g. a live buffer_size
    # edit) reconfigure monitoring through this same function without knowing
    # anything about the room, and must not silently drop the relay a room
    # peer is still listening on.
    if relay_needed is not None:
        _monitor_relay_needed = relay_needed
    return _monitor_control.run_sync(
        lambda: _configure_monitoring(settings, adopt_driver_buffer=adopt_driver_buffer),
        enabled=settings.monitoring_enabled,
    )


def _configure_monitoring(settings, *, adopt_driver_buffer: bool = False) -> None:
    _stop_monitoring_process()
    _monitor_control.check()
    from app.services import recording_service
    if recording_service.apply_monitor_settings(
        settings, "shared", _monitor_effects_disabled
    ):
        _monitor_control.publish(state="running" if settings.monitoring_enabled else "idle",
                                 engine="recording", mode="shared")
        return
    if not settings.monitoring_enabled:
        _monitor_control.publish(state="idle")
        return
    if settings.audio_driver == "asio":
        try:
            _start_asio_monitor(settings, adopt_driver_buffer=adopt_driver_buffer)
        except MonitorCancelled:
            raise
        except Exception as exc:
            logger.warning(
                "ASIO monitor failed; continuing with Windows shared audio: driver=%s error=%s",
                settings.asio_driver_name,
                exc,
            )
            _monitor_control.event(
                None,
                {
                    "event": "fallback",
                    "cause": "asio-start",
                    "message": str(exc),
                    # Named for what they mean, not for the direction of the
                    # switch: this is the driver the user asked for and that
                    # just failed to start, not the one now running (that's
                    # "auto"/shared -- the "started" event that follows fills
                    # in engine/host_api/mode for whatever actually opened).
                    "requested_driver": settings.asio_driver_name,
                    "failed_driver": settings.asio_driver_name,
                },
            )
            _start_shared_monitor(settings, driver="auto", relay_needed=_monitor_relay_needed)
        return
    _start_shared_monitor(settings, driver=settings.audio_driver, relay_needed=_monitor_relay_needed)


def _start_shared_monitor(settings, *, driver: str, relay_needed: bool = False) -> None:
    if not _AUDIO_BACKEND_AVAILABLE:
        raise RuntimeError("Audio backend is unavailable")

    devices = sd.query_devices()
    _monitor_control.check()
    input_device_id = preferred_input_device(
        settings.input_device_id,
        driver,
        settings.asio_driver_name,
        devices=devices,
        device_name=getattr(settings, "input_device_name", None),
    )
    output_device_id, resolved_input_id = (
        preferred_output_device(
            input_device_id,
            driver,
            settings.output_device_id,
            settings.asio_driver_name,
            devices=devices,
            device_name=getattr(settings, "output_device_name", None),
        ),
        _resolved_device_index(input_device_id, "input", devices),
    )
    resolved_output_id, input_info = (
        _resolved_device_index(output_device_id, "output", devices),
        devices[resolved_input_id],
    )
    output_info = devices[resolved_output_id]

    if int(input_info["hostapi"]) != int(output_info["hostapi"]):
        matched_output_id = _matching_output_for_input(resolved_input_id, None, devices)
        matched_info = devices[matched_output_id]
        if int(matched_info["hostapi"]) == int(input_info["hostapi"]):
            resolved_output_id, output_info = matched_output_id, matched_info
        else:
            raise RuntimeError(
                "Microphone and speakers use incompatible audio drivers "
                f"({_host_api_name(input_info)} vs {_host_api_name(output_info)}); "
                "select matching devices in audio settings."
            )
    output_channels = min(2, int(output_info["max_output_channels"]))
    if output_channels < 1:
        raise RuntimeError("No output device is available for microphone monitoring")
    gain = max(0.0, min(4.0, settings.volume))
    wasapi = _is_wasapi_device(input_info)
    wasapi_mode = "shared" if wasapi else "plain"
    _monitor_control.publish(
        input_device=str(input_info.get("name", "")), output_device=str(output_info.get("name", "")),
        host_api=_host_api_name(input_info), requested_blocksize=settings.buffer_size,
    )
    effects = {
        name: 0.0 if _monitor_effects_disabled else clamp01(getattr(settings, name))
        for name in ("reverb", "echo", "delay")
    }
    worker_options = {
        "input_device_id": resolved_input_id,
        "output_device_id": resolved_output_id,
        "sample_rate": _monitor_sample_rate(resolved_input_id, resolved_output_id, devices),
        "output_channels": output_channels,
        "blocksize": settings.buffer_size if wasapi else max(settings.buffer_size, _PLAIN_HOST_MIN_BLOCKSIZE),
        "gain": gain,
        **effects,
        "octave": 0.0 if _monitor_effects_disabled else max(
            -1.0, min(1.0, float(getattr(settings, "octave", 0.0) or 0.0))
        ),
        "noise_suppression": 0.0 if _monitor_effects_disabled else clamp01(
            settings.noise_suppression if settings.noise_suppression is not None else 0.35
        ),
        # Nothing left in the DSP chain to justify the Python round-trip: a
        # "no effects" monitoring session (the plain Settings "Мониторинг"
        # toggle) and an explicit "listen to raw voice" check are the same
        # request as far as the native engine is concerned, so both arm the
        # C++ raw pass-through (Engine::raw_active) from the very first block
        # instead of only after a later live update -- see
        # _native_stream_target in monitor_worker.py.
        "dry_monitor": 1.0 if (_monitor_effects_disabled or _monitor_dry_bypass) else 0.0,
        "wasapi_mode": wasapi_mode,
    }
    if wasapi:
        worker_options.update(native_shared=True, input_device_name=str(input_info["name"]),
                              output_device_name=str(output_info["name"]))
    # The relay is a room-broadcast feature (see _open_monitor_relay) -- opening
    # it and keeping a live loopback connection running costs a numpy copy plus
    # a frame-encode on every single audio block, for the whole lifetime of the
    # stream. Only pay that for the one caller that can actually have a
    # subscriber (room mode with the Python relay); solo monitoring and plain
    # recording never do, and skip it entirely rather than opening a socket
    # nothing will ever read from. (_stop_monitoring_process() above already
    # closed any previous relay, so simply not reopening one is enough here.)
    if relay_needed:
        worker_options["audio_relay_port"] = _open_monitor_relay().port
    _start_monitor_worker(worker_options)


def _open_monitor_relay() -> AudioRelayServer:
    """(Re)opens the loopback relay server for the monitor worker about to
    start. Only used by the Python worker path (_start_shared_monitor) --
    the ASIO bridge (_start_asio_monitor) is a separate native binary with no
    relay support, so ASIO users keep the JS-graph room path (see A3).
    """
    global _monitor_relay
    with _monitor_lock:
        previous = _monitor_relay
        relay = AudioRelayServer()
        _monitor_relay = relay
    if previous is not None:
        previous.close()
    return relay


def _persist_negotiated_buffer_size(buffer_size: int) -> None:
    """Writes the ASIO driver's own negotiated buffer size back into the
    singleton AudioSettings row after a driver-initiated reset (see
    _start_asio_monitor's adopt_driver_buffer path). Without this, changing
    the buffer in the driver's own control panel (ASIO4ALL, an interface's
    mixer app) would apply for the running session but silently revert to
    this app's previous saved value on the next ordinary restart.
    """
    from database import SessionLocal
    db = SessionLocal()
    try:
        settings = _get_or_create_settings(db)
        if settings.buffer_size != buffer_size:
            settings.buffer_size = buffer_size
            commit_refresh(db, settings)
    finally:
        db.close()


def _start_asio_monitor(settings: models.AudioSettings, *, adopt_driver_buffer: bool = False) -> None:
    bridge = _asio_bridge_path()
    if not bridge.is_file():
        raise RuntimeError("Native ASIO bridge is not built")
    drivers = list_asio_drivers()
    if settings.asio_driver_name not in drivers:
        raise RuntimeError("Selected ASIO driver is unavailable")
    command = [
        str(bridge),
        "--driver",
        settings.asio_driver_name,
        "--buffer-size",
        # 0 is the bridge's sentinel for "use the driver's own preferred
        # size" (see resolve_buffer_size in bridge_main.cpp) -- used only
        # right after the driver itself requested a reset, so a control-panel
        # buffer change actually takes effect instead of this app re-asserting
        # its own last-saved value straight back at the driver.
        "0" if adopt_driver_buffer else str(settings.buffer_size),
        "--sample-rate",
        str(config.RECORDING_SAMPLE_RATE),
        "--gain",
        str(max(0.0, min(4.0, settings.volume))),
        "--reverb",
        str(0.0 if _monitor_effects_disabled else clamp01(settings.reverb)),
        "--echo",
        str(0.0 if _monitor_effects_disabled else clamp01(settings.echo)),
        "--delay",
        str(0.0 if _monitor_effects_disabled else clamp01(settings.delay)),
        "--noise-suppression",
        str(0.0 if _monitor_effects_disabled else
            clamp01(settings.noise_suppression if settings.noise_suppression is not None else 0.35)
        ),
        "--octave",
        str(0.0 if _monitor_effects_disabled else max(
            -1.0, min(1.0, float(getattr(settings, "octave", 0.0) or 0.0))
        )),
    ]
    # kAsioResetRequest fires when the driver's own control panel changes
    # something (buffer size, sample rate) out from under the running stream;
    # the ASIO SDK's contract for that message is to close and reopen the
    # driver, which is exactly what re-running request_monitoring does
    # (through the normal coalescing lane, so it plays correctly with any
    # concurrent user-initiated stop/settings change instead of racing it).
    # settings itself may be a live, DB-session-bound AudioSettings row (some
    # callers of configure_monitoring pass one directly, not the detached
    # snapshot request_monitoring builds for itself) -- that session can be
    # long closed by the time a reset actually happens, so a fresh detached
    # snapshot is captured here rather than closing over settings as-is.
    reset_snapshot = settings_snapshot(settings)
    _launch_monitor_process(
        command,
        cwd=bridge.parent,
        on_driver_reset=lambda: request_monitoring(reset_snapshot, adopt_driver_buffer=True),
        on_buffer_negotiated=_persist_negotiated_buffer_size if adopt_driver_buffer else None,
    )


def _start_monitor_worker(worker_options: dict) -> None:
    if config.IS_FROZEN:
        command = [sys.executable, "--audio-monitor", "--config", json.dumps(worker_options)]
    else:
        command = [
            sys.executable,
            "-m",
            "app.services.monitor_worker",
            "--config",
            json.dumps(worker_options),
        ]
    _launch_monitor_process(command, cwd=Path(config.BASE_DIR))


def _launch_monitor_process(
    command: list[str], *, cwd: Path, on_driver_reset=None, on_buffer_negotiated=None
) -> None:
    global _monitor_process, _monitor_reader
    ready = threading.Event()
    state: dict[str, str | None] = {"error": None, "stage": "process bootstrap (before Python entry point)"}
    launched_at = time.monotonic()
    _monitor_control.check()
    token = getattr(_monitor_control.local, "token", None)
    # Deliberately plain process priority, not HIGH_PRIORITY_CLASS: that used
    # to boost every thread in this process (NumPy/relay/JSON/stdout, all of
    # it), not just the realtime audio path -- which already gets its own
    # targeted boost via AvSetMmThreadCharacteristicsW("Pro Audio") on the
    # native WASAPI engine's pump thread (see monitor.cpp), and via
    # PortAudio's own internal WASAPI thread-priority handling for the other
    # engines. Elevating the whole process on top of that only ever competed
    # with Electron and everything else for CPU on a weaker machine, with no
    # audio benefit.
    #
    # "Plain" must still be explicit: with no priority class flag at all,
    # Windows has this child inherit the CALLING process's CURRENT priority
    # class, not always NORMAL. If a song was processing (or had just
    # finished) recently enough that pipeline_service._configure_ai_runtime's
    # BELOW_NORMAL_PRIORITY_CLASS on the backend process hadn't been restored
    # yet, every monitor/ASIO-bridge subprocess launched during that window
    # would start -- and silently stay, since nothing ever revisits a child's
    # priority after it launches -- at BELOW_NORMAL for its whole session,
    # for however long the user kept monitoring going. NORMAL_PRIORITY_CLASS
    # pins the actually-intended "plain" priority regardless of what the
    # parent's own priority happens to be at that moment.
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "NORMAL_PRIORITY_CLASS", 0)
    )
    # Registration and stop are atomic. A release response must not race a
    # just-created child that has not yet been installed in _monitor_process.
    with _monitor_lock:
        _monitor_control.check()
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        _monitor_process = process
        _monitor_reader = None

    def consume_output() -> None:
        assert process.stdout is not None
        reset_requested = False
        for line in process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Audio monitor worker: %s", line.rstrip())
                continue
            if not isinstance(message, dict):
                continue
            event = message.get("event")
            if event == "stage":
                state["stage"] = str(message.get("stage") or "unknown")[:200]
                logger.info("Audio monitor startup: stage=%s elapsed_sec=%.2f", state["stage"], time.monotonic() - launched_at)
            with _monitor_lock:
                current = _monitor_process is process
            if current:
                _monitor_control.event(token, message)
            if event == "started":
                # The ASIO bridge's "started" event uses driver/buffer_size/
                # latency_source keys; the WASAPI worker's uses
                # engine/blocksize/latency. Fall back across both schemas so
                # this line reports real values for either, instead of
                # silently logging None for whichever one is not currently
                # running.
                #
                # For the native WASAPI engine specifically, "blocksize" is
                # just the requested value echoed back (see Info.blocksize in
                # backend/engines/wasapi/monitor.cpp) -- the period actually
                # negotiated with the device (clamped into its own
                # min/max/fundamental via GetSharedModeEnginePeriod) is
                # input_period_frames/output_period_frames instead. Prefer
                # that so this line reports what really applied, not just
                # what was asked for.
                logger.info(
                    "Audio monitor started: driver=%s buffer_size=%s sample_rate=%s latency=%s",
                    message.get("driver", message.get("engine")),
                    message.get(
                        "buffer_size",
                        message.get("input_period_frames", message.get("blocksize")),
                    ),
                    message.get("sample_rate"),
                    message.get("latency", message.get("latency_source")),
                )
                # The driver is already up and running at this point -- the
                # parent must learn that (ready.set()) before this does any
                # SQLite work, not after. on_buffer_negotiated commits a
                # write (see _persist_negotiated_buffer_size); a slow/locked
                # database previously delayed ready being set at all, and a
                # startup timeout waiting on it could kill an otherwise
                # perfectly healthy ASIO stream.
                ready.set()
                if on_buffer_negotiated is not None:
                    negotiated = message.get("buffer_size")
                    if isinstance(negotiated, (int, float)) and not isinstance(negotiated, bool) and negotiated > 0:
                        on_buffer_negotiated(int(negotiated))
            elif event == "fallback":
                logger.warning(
                    "Audio monitor selected a safer fallback: %s (blocksize=%s latency=%s)",
                    message.get("message"),
                    message.get("blocksize"),
                    message.get("latency"),
                )
            elif event == "level":
                with _monitor_lock:
                    if _monitor_process is process:
                        _monitor_signal.update(
                            {key: message[key] for key in _monitor_signal if key in message}
                        )
            elif event == "error":
                state["error"] = str(message.get("message") or "unknown audio worker error")
                ready.set()
            elif event == "stopped":
                reset_requested = bool(message.get("reset_requested"))
        if not ready.is_set():
            state["error"] = state["error"] or "audio monitoring worker terminated during startup"
            ready.set()
        with _monitor_lock:
            superseded = _monitor_process is not process
        if superseded:
            return
        if reset_requested and on_driver_reset is not None:
            # The driver reset itself (e.g. its control panel's buffer size
            # changed), not a stop we asked for -- _monitor_process is still
            # this process (checked above), so this is not racing a
            # concurrent stop/settings change. Restart through the normal
            # coalescing lane instead of reporting a monitoring failure.
            logger.info("ASIO driver requested a reset; restarting the monitor to pick up its new settings")
            on_driver_reset()
            return
        with _monitor_lock:
            if _monitor_process is process:
                _monitor_signal.update(_EMPTY_MONITOR_SIGNAL)
                _monitor_control.publish(token, state="error", error=state["error"] or "Audio monitoring worker exited")

    reader = threading.Thread(target=consume_output, name="audio-monitor-reader", daemon=True)
    try:
        with _monitor_lock:
            _monitor_control.check()
            if _monitor_process is not process:
                raise MonitorCancelled("Monitoring process was released")
            _monitor_reader = reader
            reader.start()
        if token is None:
            started = ready.wait(timeout=_MONITOR_START_TIMEOUT_SECONDS)
        else:
            deadline = time.monotonic() + _MONITOR_START_TIMEOUT_SECONDS
            started = False
            while time.monotonic() < deadline:
                _monitor_control.check()
                if ready.wait(timeout=0.05):
                    started = True
                    break
        _monitor_control.check()
        if not started:
            raise RuntimeError(
                "Timed out starting direct microphone monitoring: "
                f"stage={state['stage']}; elapsed_sec={time.monotonic() - launched_at:.2f}; "
                f"worker={Path(command[0]).name}; exit_code={process.poll()}"
            )
        if state["error"]:
            raise RuntimeError(f"Could not start direct microphone monitoring: {state['error']}")
    except Exception:
        _stop_monitoring_process(expected_process=process)
        raise


def is_monitor_process_alive() -> bool:
    """Whether check_signal_quality would take its cached-signal fast path.

    Lets a caller that's about to resolve an input device purely to hand it
    to check_signal_quality skip that resolution (a full PortAudio device
    enumeration) when the result would go unused: check_signal_quality never
    looks at device_id at all once a live monitor process means it can
    answer from the already-running worker's own periodic reports instead.
    """
    with _monitor_lock:
        return _monitor_process is not None and _monitor_process.poll() is None


def check_signal_quality(
    device_id: int | None,
    gain: float = 1.0,
    duration_sec: float = 0.5,
    monitoring_expected: bool = False,
) -> dict:
    global _monitor_process, _monitor_reader
    if not _AUDIO_BACKEND_AVAILABLE:
        raise RuntimeError("Аудио-бэкенд (sounddevice) недоступен")

    from app.services import recording_service
    signal = recording_service.capture_signal()
    if signal is not None:
        return signal

    dead_process, dead_reader = None, None
    with _monitor_lock:
        process = _monitor_process
        if process is not None and process.poll() is None:
            return dict(_monitor_signal)
        if process is not None:
            dead_process, dead_reader = process, _monitor_reader
            _monitor_process = None
            _monitor_reader = None
            _monitor_signal.update(_EMPTY_MONITOR_SIGNAL)
        stopping = monitoring_expected or monitoring_status()["state"] in {"starting", "stopping"}
    if dead_process is not None:
        # The worker exited on its own (driver crash, device unplugged) --
        # nothing else calls _stop_monitoring_process for this case, so its
        # pipes/reader thread need the same cleanup here, not just clearing
        # the _monitor_process reference (see _stop_monitoring_process for
        # the same class of leak on the ordinary stop path).
        if dead_reader is not None and dead_reader is not threading.current_thread():
            dead_reader.join(timeout=1.0)
        if dead_process.stdout is not None:
            dead_process.stdout.close()
        if dead_process.stdin is not None:
            with contextlib.suppress(OSError):
                dead_process.stdin.close()
    if stopping:
        return dict(_monitor_signal)

    # Settings polls this every 80ms (see runtime-config's realtimeSignal)
    # purely for a pre-monitoring level preview; a fresh sd.rec()+sd.wait()
    # actually opens and closes the input device for real, every single poll,
    # for as long as the page stays open with monitoring off. That competed
    # for the device with whatever the user does next (pressing "Monitoring"
    # itself, or ASIO opening exclusively) and kept the device busy almost
    # continuously. A cached result between real probes keeps the preview
    # responsive without hammering the device the whole time the page is open.
    if (
        time.monotonic() - _signal_probe_cache["at"] < _SIGNAL_PROBE_INTERVAL_SEC
        and _signal_probe_cache["device"] == device_id
        and _signal_probe_cache["gain"] == gain
        and _signal_probe_cache["result"] is not None
    ):
        return dict(_signal_probe_cache["result"])

    resolved_device = device_id
    rates: list[int] = []
    try:
        resolved_device = _resolved_device_index(device_id, "input")
        default_rate = int(
            round(float(sd.query_devices(resolved_device).get("default_samplerate", 0) or 0))
        )
        if default_rate > 0:
            rates.append(default_rate)
    except Exception:
        # The capture attempts below still produce the useful backend-specific
        # error if the selected device disappeared between settings and polling.
        pass
    rates.extend((44_100, 48_000, 16_000))
    last_error: Exception | None = None
    recording = None
    def _capture(rate: int):
        result = sd.rec(
            int(duration_sec * rate),
            samplerate=rate,
            channels=1,
            device=resolved_device,
            dtype="float32",
        )
        sd.wait()
        return result

    for sample_rate in dict.fromkeys(rates):
        try:
            recording = run_on_audio_thread(_capture, sample_rate)
            break
        except Exception as exc:  # PortAudio errors depend on the Windows host API.
            last_error = exc
    if recording is None:
        raise RuntimeError(f"Could not read microphone signal: {last_error}") from last_error
    samples = np.clip(recording.flatten() * max(0.0, min(4.0, gain)), -1.0, 1.0)

    rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
    rms_db, peak = (
        20 * np.log10(rms) if rms > 0 else -120.0,
        float(np.max(np.abs(samples))) if len(samples) else 0.0,
    )

    result = {
        "rms_db": round(rms_db, 1),
        "clipping": peak >= 0.99,
        "silent": rms_db < -50.0,
    }
    # Timestamped now, after the probe actually completed -- not before it
    # (sd.rec()+sd.wait(), possibly retried across several sample rates)
    # started. Stamping it early meant the cache window was already partly
    # spent by the time this result existed, and a slow probe could make it
    # stale before it was ever even cached.
    _signal_probe_cache.update(at=time.monotonic(), device=device_id, gain=gain, result=result)
    return result
