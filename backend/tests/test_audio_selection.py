import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.services import audio_service
from tests._shared import patch_attrs, patch_many, raises


def device(
    name, hostapi, *, inputs=0, outputs=0, rate=48000, input_latency=0.02, output_latency=0.02
):
    return {
        "name": name,
        "hostapi": hostapi,
        "max_input_channels": inputs,
        "max_output_channels": outputs,
        "default_samplerate": rate,
        "default_low_input_latency": input_latency,
        "default_low_output_latency": output_latency,
    }


def install_devices(monkeypatch, devices, hosts):
    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", True)
    patch_attrs(
        monkeypatch,
        audio_service.sd,
        query_devices=Mock(
            side_effect=lambda index=None, **_kwargs: devices if index is None else devices[index]
        ),
        query_hostapis=lambda index: {"name": hosts[index]},
    )


def test_asio_bridge_path_and_driver_enumeration(monkeypatch, tmp_path):
    bridge = tmp_path / "KaraokeAsioBridge.exe"
    patch_many(
        monkeypatch,
        (audio_service.config, "IS_FROZEN", True),
        (audio_service.sys, "executable", str(tmp_path / "Backend.exe")),
    )
    assert (audio_service._asio_bridge_path() == bridge) and (
        audio_service.list_asio_drivers() == []
    )

    bridge.write_bytes(b"bridge")
    run = Mock(
        return_value=SimpleNamespace(stdout='log\n{"drivers":["Studio ASIO", 4, "USB ASIO"]}\n')
    )
    monkeypatch.setattr(audio_service.subprocess, "run", run)
    assert audio_service.list_asio_drivers() == ["Studio ASIO", "USB ASIO"]
    run.side_effect = OSError("cannot launch")
    assert audio_service.list_asio_drivers() == []

    patch_attrs(monkeypatch, audio_service.config, IS_FROZEN=False, PROJECT_ROOT=tmp_path)
    assert audio_service._asio_bridge_path() == tmp_path / "generated/build/asio/KaraokeAsioBridge.exe"


def test_device_token_latency_and_asio_hint_normalization():
    assert (
        (
            audio_service._device_tokens("USB Audio Device Microphone Pro")
            == {"usb", "microphone", "pro"}
        )
        and (audio_service._device_latency({"default_low_input_latency": "0.01"}, "input") == 0.01)
        and (audio_service._device_latency({"default_low_input_latency": -1}, "input") == 0)
        and (audio_service._device_latency({"default_low_input_latency": "bad"}, "input") == 1)
        and (audio_service._asio_device_hint(None) == "")
        and (audio_service._asio_device_hint("  Focusrite USB ASIO Driver ") == "focusrite usb")
    )


def test_resolved_device_prefers_default_then_low_latency_host(monkeypatch):
    devices = [
        device("Slow", 0, inputs=1, input_latency=0.1),
        device("Fast", 1, inputs=1, input_latency=0.01),
    ]
    install_devices(monkeypatch, devices, {0: "MME", 1: "Windows WASAPI"})
    monkeypatch.setattr(audio_service.sd.default, "device", (1, -1))
    assert (audio_service._resolved_device_index(None, "input") == 1) and (
        audio_service._resolved_device_index(0, "input") == 0
    )

    monkeypatch.setattr(audio_service.sd.default, "device", (-1, -1))
    assert audio_service._resolved_device_index(None, "input") == 1
    raises(
        RuntimeError,
        lambda: audio_service._resolved_device_index(None, "output"),
        match="No output",
    )


def test_stale_saved_device_id_is_recovered_by_its_saved_name(monkeypatch):
    # A USB interface reconnecting (or the app restarting after Windows
    # renumbered devices) commonly lands the same physical device at a
    # different PortAudio index. Once the raw saved index no longer resolves
    # to it, the saved name must be tried before falling through to whatever
    # the system default happens to be -- otherwise the app silently drops
    # back to onboard audio instead of keeping the USB card selected.
    devices = [
        device("USB Interface Mic", 0, inputs=2, input_latency=0.01),
        device("Onboard Mic", 1, inputs=1, input_latency=0.01),
    ]
    install_devices(monkeypatch, devices, {0: "Windows WASAPI", 1: "Windows WASAPI"})
    monkeypatch.setattr(audio_service.sd.default, "device", (1, -1))

    # Stale index 5 does not exist; without a saved name this must fall back
    # to the system default (existing behavior, unchanged).
    assert audio_service._resolved_device_index(5, "input") == 1
    # With the saved name, the USB interface is found again by name instead.
    assert (
        audio_service._resolved_device_index(5, "input", preferred_name="USB Interface Mic") == 0
    )
    # A saved name that matches nothing currently connected still falls back
    # to the system default rather than raising.
    assert (
        audio_service._resolved_device_index(5, "input", preferred_name="Missing Device") == 1
    )
    # preferred_input_device threads device_name through the same path.
    assert (
        audio_service.preferred_input_device(5, "auto", device_name="USB Interface Mic") == 0
    )


def test_resolved_device_never_lands_on_wdm_ks(monkeypatch):
    # A stream opened against a WDM-KS-hosted device can fail outright on some
    # drivers ("Unanticipated host error" / DeviceIoControl on
    # KSPROPERTY_PIN_PHYSICALCONNECTION) -- an explicitly saved device id, the
    # system default, and the ranked fallback must all skip it, never trust
    # it just because its index/capability check would otherwise pass.
    devices = [
        device("Mic (WDM-KS)", 0, inputs=1, input_latency=0.001),
        device("Mic (MME)", 1, inputs=1, input_latency=0.2),
    ]
    install_devices(monkeypatch, devices, {0: "Windows WDM-KS", 1: "MME"})

    # An explicit, otherwise-valid saved id pointing at the WDM-KS entry.
    monkeypatch.setattr(audio_service.sd.default, "device", (-1, -1))
    assert audio_service._resolved_device_index(0, "input") == 1

    # The system default itself resolves to the WDM-KS entry.
    monkeypatch.setattr(audio_service.sd.default, "device", (0, -1))
    assert audio_service._resolved_device_index(None, "input") == 1

    # No explicit id, no usable default: the ranked-candidate fallback must
    # still exclude WDM-KS rather than pick it as a last resort.
    monkeypatch.setattr(audio_service.sd.default, "device", (-1, -1))
    assert audio_service._resolved_device_index(None, "input") == 1

    # If literally every capable device is WDM-KS-hosted, fail clearly
    # instead of silently opening a stream that will crash.
    only_wdm_ks = [device("Mic (WDM-KS)", 0, inputs=1)]
    install_devices(monkeypatch, only_wdm_ks, {0: "Windows WDM-KS"})
    raises(
        RuntimeError,
        lambda: audio_service._resolved_device_index(0, "input"),
        match="No input",
    )


def test_low_latency_and_duplex_selection_match_physical_endpoint(monkeypatch):
    devices = [
        device("USB Studio Mic", 0, inputs=1),
        device("USB Studio Mic", 1, inputs=1, input_latency=0.005),
        device("USB Studio Speakers", 1, outputs=2, output_latency=0.006),
        device("HDMI Display", 0, outputs=2),
    ]
    install_devices(monkeypatch, devices, {0: "MME", 1: "Windows WASAPI"})
    assert (
        (audio_service._low_latency_equivalent(0, "input") == 1)
        and (audio_service._matching_output_for_input(1, None) == 2)
        and (audio_service._low_latency_equivalent(3, "output") == 3)
    )


def test_mme_driver_stays_on_mme_instead_of_upgrading_to_wasapi(monkeypatch):
    devices = [
        device("USB Studio Mic", 0, inputs=1),
        device("USB Studio Mic", 1, inputs=1, input_latency=0.005),
        device("USB Studio Speakers", 0, outputs=2),
        device("USB Studio Speakers", 1, outputs=2, output_latency=0.006),
    ]
    install_devices(monkeypatch, devices, {0: "MME", 1: "Windows WASAPI"})

    # "auto" upgrades an explicitly selected MME device to its same-named
    # WASAPI equivalent (existing behavior).
    assert audio_service.preferred_input_device(0, "auto") == 1
    # "mme" must not -- otherwise there would be no way to actually monitor
    # (and compare latency) on plain MME at all.
    assert audio_service.preferred_input_device(0, "mme") == 0
    assert audio_service._low_latency_equivalent(0, "input", preferred_host_api="mme") == 0
    assert audio_service.preferred_output_device(0, "mme", None) == 2


def test_auto_output_keeps_exact_interface_port_name(monkeypatch):
    devices = [
        device("Analogue 1/2 (6- Audient iD14)", 0, inputs=2),
        device("Analogue 1/2 (6- Audient iD14)", 0, outputs=2),
        device("Analogue 5/6 (6- Audient iD14)", 1, outputs=2, output_latency=0.003),
        device("Analogue 1/2 (6- Audient iD14)", 1, inputs=2, input_latency=0.003),
        device("Analogue 1/2 (6- Audient iD14)", 1, outputs=2, output_latency=0.003),
    ]
    install_devices(monkeypatch, devices, {0: "MME", 1: "Windows WASAPI"})

    assert audio_service._low_latency_equivalent(1, "output") == 4
    assert audio_service._matching_output_for_input(3, None) == 4


def test_duplex_selection_recovers_original_output_on_matching_host(monkeypatch):
    devices = [
        device("Mic", 0, inputs=1),
        device("Selected speakers", 0, outputs=2),
        device("Mapped speakers", 1, outputs=2),
    ]
    install_devices(monkeypatch, devices, {0: "MME", 1: "Windows WASAPI"})
    monkeypatch.setattr(audio_service, "_low_latency_equivalent", Mock(return_value=2))
    assert audio_service._matching_output_for_input(0, 1) == 1

    devices[1]["hostapi"] = 1
    assert audio_service._matching_output_for_input(0, 1) == 1
    devices[2]["hostapi"] = 0
    assert audio_service._matching_output_for_input(0, 1) == 2


def test_asio_matching_and_preferred_device_fallbacks(monkeypatch):
    devices = [
        device("Focusrite USB Audio", 0, inputs=2, outputs=2),
        device("Focusrite USB ASIO", 1, inputs=2, outputs=2),
    ]
    install_devices(monkeypatch, devices, {0: "MME", 1: "ASIO"})
    assert (
        (audio_service._matching_asio_device_index("Focusrite USB ASIO", "input") == 1)
        and (audio_service.preferred_input_device(None, "asio", "Focusrite USB ASIO") == 1)
        # A saved id (0) that is not actually on the ASIO host API -- e.g.
        # left over from a previous "auto"/WASAPI driver selection -- must
        # never be trusted for an ASIO session: it would make PortAudio
        # recording open a different physical device than the one the native
        # ASIO bridge is monitoring through. Re-matched to the real ASIO
        # device (1) by name instead.
        and (audio_service.preferred_input_device(0, "asio", "Focusrite USB ASIO") == 1)
        and (audio_service.preferred_output_device(1, "asio", None, "Focusrite USB ASIO") == 1)
        and (audio_service.preferred_output_device(None, "asio", 1, "Focusrite USB ASIO") == 1)
        and (audio_service.preferred_output_device(None, "asio", 0, None) is None)
        and (audio_service.preferred_sample_rate(0) == 48000)
    )

    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", False)
    assert (
        (audio_service._matching_asio_device_index("Focusrite", "input") is None)
        and (audio_service.preferred_input_device(4) == 4)
        and (audio_service.preferred_output_device(output_device_id=5) == 5)
        and (audio_service.preferred_sample_rate(0) == audio_service.config.RECORDING_SAMPLE_RATE)
    )


def test_asio_input_resolution_never_lands_on_wdm_ks(monkeypatch):
    # Every host API PortAudio exposes for the same interface shares its
    # product name, so a same-named WDM-KS entry can otherwise out-score (or
    # simply stand in for) the real ASIO one -- and its pin can fail outright
    # just being opened for recording (see _is_wdm_ks_device).
    devices = [
        device("Audient iD4", 0, inputs=2, outputs=2),  # Windows WDM-KS
        device("Audient iD4", 1, inputs=2, outputs=2),  # ASIO
    ]
    install_devices(monkeypatch, devices, {0: "Windows WDM-KS", 1: "ASIO"})

    # Name-matched search skips the WDM-KS entry even though it matches too.
    assert audio_service._matching_asio_device_index("Audient iD4", "input") == 1
    # No saved device id: falls through to the (WDM-KS-free) name match.
    assert audio_service.preferred_input_device(None, "asio", "Audient iD4") == 1
    # A saved device id that now happens to resolve to the WDM-KS entry (e.g.
    # after the device list reordered) is not trusted blindly either --
    # falls back to the same name match instead of the broken pin.
    assert audio_service.preferred_input_device(0, "asio", "Audient iD4") == 1
    # A saved id pointing at the real ASIO entry is still honored as-is.
    assert audio_service.preferred_input_device(1, "asio", "Audient iD4") == 1


def test_preferred_sample_rate_trusts_the_asio_bridge_over_the_stale_device_table(monkeypatch):
    # PortAudio's device table is captured once per process and never
    # refreshed, but the ASIO bridge can change the interface's actual clock
    # when monitoring starts (ASIOSetSampleRate). A stale 48000 here must not
    # win once the bridge has reported the real negotiated rate back -- that
    # exact mismatch is what stamped recordings with the wrong sample rate.
    devices = [device("Focusrite USB ASIO", 0, inputs=2, outputs=2, rate=48000)]
    install_devices(monkeypatch, devices, {0: "ASIO"})
    monkeypatch.setattr(
        audio_service._monitor_control,
        "snapshot",
        lambda: {"mode": "ASIO", "state": "running", "sample_rate": 44100.0},
    )
    assert audio_service.preferred_sample_rate(0, "asio") == 44100


def test_preferred_sample_rate_falls_back_when_asio_monitor_is_not_running(monkeypatch):
    devices = [device("Focusrite USB ASIO", 0, inputs=2, outputs=2, rate=48000)]
    install_devices(monkeypatch, devices, {0: "ASIO"})
    monkeypatch.setattr(audio_service._monitor_control, "snapshot", lambda: {"state": "idle"})
    assert audio_service.preferred_sample_rate(0, "asio") == 48000


def test_preferred_sample_rate_ignores_a_running_non_asio_monitor(monkeypatch):
    devices = [device("Focusrite USB ASIO", 0, inputs=2, outputs=2, rate=48000)]
    install_devices(monkeypatch, devices, {0: "ASIO"})
    # A running WASAPI/generic monitor worker must never be mistaken for the
    # ASIO bridge's own reported rate, even if it happens to report one.
    monkeypatch.setattr(
        audio_service._monitor_control,
        "snapshot",
        lambda: {"mode": "shared", "state": "running", "sample_rate": 44100.0},
    )
    assert audio_service.preferred_sample_rate(0, "asio") == 48000


def test_asio_matching_rejects_empty_or_unrelated_names(monkeypatch):
    devices = [device("Generic microphone", 0, inputs=1)]
    install_devices(monkeypatch, devices, {0: "MME"})
    assert (
        (audio_service._matching_asio_device_index(None, "input") is None)
        and (audio_service._matching_asio_device_index("Focusrite ASIO", "input") is None)
        and (audio_service._matching_asio_device_index("Generic ASIO", "output") is None)
    )

    monkeypatch.setattr(audio_service, "_matching_asio_device_index", Mock(return_value=4))
    assert audio_service.preferred_output_device(None, "asio", None, "Studio") == 4

    monkeypatch.setattr(audio_service, "_low_latency_equivalent", Mock(return_value=3))
    assert (audio_service.preferred_input_device(None, "auto") == 3) and (
        audio_service.preferred_output_device(0, "asio", None, "Studio") == 4
    )
    monkeypatch.setattr(audio_service, "_matching_output_for_input", Mock(return_value=7))
    assert audio_service.preferred_output_device(0, "auto", 2) == 7
    audio_service._matching_output_for_input.assert_called_once_with(3, 2, None, preferred_name=None)

    # A saved output_device_id is a PortAudio index, not a stable identity --
    # its saved name (device_name) must reach the name-recovery path the
    # same way it already does for input (see preferred_input_device).
    audio_service._matching_output_for_input.reset_mock()
    assert audio_service.preferred_output_device(0, "auto", 2, device_name="Old Speakers") == 7
    audio_service._matching_output_for_input.assert_called_once_with(
        3, 2, None, preferred_name="Old Speakers"
    )


def test_monitor_sample_rate_prefers_common_48khz_and_falls_back_to_input(monkeypatch):
    devices = {
        1: device("USB microphone", 0, inputs=1, rate=44_100),
        2: device("Speakers", 0, outputs=2, rate=48_000),
    }
    checked = []
    patch_attrs(
        monkeypatch,
        audio_service.sd,
        query_devices=lambda index: devices[index],
        check_input_settings=lambda **kwargs: checked.append(("input", kwargs["samplerate"])),
        check_output_settings=lambda **kwargs: checked.append(("output", kwargs["samplerate"])),
    )

    assert audio_service._monitor_sample_rate(1, 2) == 48_000.0
    assert checked == [("input", 48_000), ("output", 48_000)]

    def reject(**_kwargs):
        raise RuntimeError("unsupported")

    patch_attrs(
        monkeypatch,
        audio_service.sd,
        check_input_settings=reject,
        check_output_settings=reject,
    )
    assert audio_service._monitor_sample_rate(1, 2) == 44_100.0


def test_monitor_sample_rate_keeps_matching_consumer_devices_at_native_rate(monkeypatch):
    devices = {
        1: device("Razer USB Sound Card microphone", 0, inputs=1, rate=44_100),
        2: device("Razer USB Sound Card headphones", 0, outputs=2, rate=44_100),
    }
    checked = []
    patch_attrs(
        monkeypatch,
        audio_service.sd,
        query_devices=lambda index: devices[index],
        check_input_settings=lambda **kwargs: checked.append(("input", kwargs["samplerate"])),
        check_output_settings=lambda **kwargs: checked.append(("output", kwargs["samplerate"])),
    )

    assert audio_service._monitor_sample_rate(1, 2) == 44_100.0
    assert checked == [("input", 44_100), ("output", 44_100)]


def test_device_lists_expose_capabilities_and_host_api(monkeypatch):
    devices = [
        device("Microphone", 0, inputs=1),
        device("Interface", 1, inputs=2, outputs=2),
    ]
    install_devices(monkeypatch, devices, {0: "MME", 1: "ASIO"})
    assert (
        audio_service.list_input_devices()
        == [
            {
                "index": 0,
                "name": "Microphone [MME]",
                "max_input_channels": 1,
                "default_samplerate": 48000,
                "host_api": "MME",
                "is_asio": False,
            },
            {
                "index": 1,
                "name": "Interface [ASIO]",
                "max_input_channels": 2,
                "default_samplerate": 48000,
                "host_api": "ASIO",
                "is_asio": True,
            },
        ]
    ) and ([item["index"] for item in audio_service.list_output_devices()] == [1])

    monkeypatch.setattr(audio_service, "_AUDIO_BACKEND_AVAILABLE", False)
    assert audio_service.list_input_devices() == []


def test_monitor_worker_command_is_serializable(monkeypatch, tmp_path):
    launch = Mock()
    monkeypatch.setattr(audio_service, "_launch_monitor_process", launch)
    patch_attrs(monkeypatch, audio_service.config, IS_FROZEN=False, BASE_DIR=tmp_path)
    options = {"input_device_id": 1, "gain": 1.5}
    audio_service._start_monitor_worker(options)
    command = launch.call_args.args[0]
    assert (command[1:4] == ["-m", "app.services.monitor_worker", "--config"]) and (
        json.loads(command[-1]) == options
    )
