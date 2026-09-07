"""Микрофон и звук."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

import schemas
from app.api.errors import http_error
from app.services import audio_service
from database import get_db

router = APIRouter(prefix="/audio", tags=["audio"])


@router.get("/devices", response_model=list[schemas.AudioDeviceOut])
def list_devices():
    return audio_service.list_input_devices()


@router.get("/output-devices", response_model=list[schemas.AudioOutputDeviceOut])
def list_output_devices():
    return audio_service.list_output_devices()


@router.get("/asio-drivers", response_model=list[schemas.AsioDriverOut])
def list_asio_drivers():
    return [{"name": name} for name in audio_service.list_asio_drivers()]


@router.get("/settings", response_model=schemas.AudioSettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return audio_service.get_settings(db)


@router.post("/settings", response_model=schemas.AudioSettingsOut)
def update_settings(patch: schemas.AudioSettingsUpdate, db: Session = Depends(get_db)):
    with http_error(RuntimeError, 503): return audio_service.update_settings(db, patch.model_dump(exclude_unset=True), background=True)


@router.post("/direct-monitor/start", response_model=schemas.AudioSettingsOut, status_code=202)
def start_direct_monitoring(db: Session = Depends(get_db), disabled_effects: bool = False):
    with http_error(RuntimeError, 503):
        return audio_service.set_monitoring_enabled(db, True, disabled_effects=disabled_effects, background=True)


@router.post("/direct-monitor/stop", response_model=schemas.AudioSettingsOut)
def stop_direct_monitoring(db: Session = Depends(get_db)):
    # Callers may immediately open recording/WebRTC capture after this response.
    # Do not acknowledge a release while the old process still owns the device.
    return audio_service.set_monitoring_enabled(db, False)


@router.post("/direct-monitor/suspend", response_model=schemas.AudioSettingsOut)
def suspend_direct_monitoring(db: Session = Depends(get_db)):
    settings = audio_service.get_settings(db)
    audio_service.suspend_monitoring()
    return settings


@router.post("/direct-monitor/dry")
def set_direct_monitor_dry(enabled: bool = True, db: Session = Depends(get_db)):
    return audio_service.set_monitor_dry_bypass(db, enabled)


@router.post("/direct-monitor/resume", response_model=schemas.AudioSettingsOut, status_code=202)
def resume_direct_monitoring(db: Session = Depends(get_db)):
    settings = audio_service.get_settings(db)
    audio_service.resume_monitoring(settings)
    return settings


@router.get("/direct-monitor/status")
def direct_monitor_status():
    return audio_service.monitoring_status()


@router.post("/devices/select", response_model=schemas.AudioSettingsOut)
def select_device(device_id: int, db: Session = Depends(get_db)):
    return audio_service.update_settings(db, {"input_device_id": device_id}, background=True)


@router.get("/signal-quality", response_model=schemas.SignalQualityOut)
def signal_quality(db: Session = Depends(get_db)):
    settings = audio_service.get_settings(db)
    with http_error(RuntimeError, 503):
        # Settings polls this every 80ms while monitoring is on (see
        # runtime-config's realtimeSignal) -- check_signal_quality answers
        # from the running worker's own cached reports in that case and
        # never looks at the device id at all, so resolving one first (a
        # full PortAudio device-table enumeration) would be pure waste on
        # every single poll for as long as the page stays open.
        #
        # Resolve the device the same way monitoring/recording do: an ASIO
        # driver selection with no explicit device saved must probe the
        # matching ASIO input, not whatever Windows treats as the default
        # WASAPI/MME device (check_signal_quality's own fallback, which has
        # no notion of the selected driver at all).
        resolved_device_id = (
            None
            if audio_service.is_monitor_process_alive()
            else audio_service.preferred_input_device(
                settings.input_device_id,
                settings.audio_driver,
                settings.asio_driver_name,
                device_name=getattr(settings, "input_device_name", None),
            )
        )
        return audio_service.check_signal_quality(
            resolved_device_id,
            gain=settings.volume,
            monitoring_expected=settings.monitoring_enabled,
        )
