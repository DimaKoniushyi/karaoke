import sys
from types import ModuleType
from unittest.mock import Mock

from AI import runtime
from AI.engines import ctc, pitch, text


class _ForbiddenCuda:
    def __getattribute__(self, name):
        raise AssertionError(f"CUDA must not be inspected in CPU mode: {name}")


def _cpu_plan(monkeypatch):
    roles = ("separation", "pitch", "asr", "ctc_ru", "ctc_uk", "aligner")
    hardware = runtime.HardwareProfile("test", 1, torch_available=True, cuda_available=True)
    selected = {role: runtime.BackendSpec("pytorch:cpu", "cpu") for role in roles}
    monkeypatch.setattr(runtime, "_plan", runtime.RuntimePlan(hardware, selected, preference="cpu"))


def _fake_torch(monkeypatch):
    torch = ModuleType("torch")
    torch.cuda = _ForbiddenCuda()
    torch.float16 = object()
    torch.float32 = object()
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def test_cpu_runtime_loads_pitch_without_inspecting_cuda(monkeypatch):
    _cpu_plan(monkeypatch)
    _fake_torch(monkeypatch)
    torchfcpe = ModuleType("torchfcpe")
    model = Mock()
    model.eval.return_value = model
    torchfcpe.spawn_bundled_infer_model = Mock(return_value=model)
    monkeypatch.setitem(sys.modules, "torchfcpe", torchfcpe)

    estimator = pitch.FCPEPitchEstimator()
    estimator._load()

    torchfcpe.spawn_bundled_infer_model.assert_called_once_with(device="cpu")
    model.eval.assert_called_once_with()


def test_cpu_runtime_loads_ctc_language_role_without_inspecting_cuda(monkeypatch):
    _cpu_plan(monkeypatch)
    _fake_torch(monkeypatch)
    transformers = ModuleType("transformers")
    processor = Mock()
    model = Mock()
    model.to.return_value = model
    model.eval.return_value = model
    transformers.Wav2Vec2Processor = Mock()
    transformers.Wav2Vec2Processor.from_pretrained.return_value = processor
    transformers.AutoModelForCTC = Mock()
    transformers.AutoModelForCTC.from_pretrained.return_value = model
    monkeypatch.setitem(sys.modules, "transformers", transformers)

    aligner = ctc.CTCWordAligner("model", "ctc_uk")
    aligner._load()

    model.to.assert_called_once_with("cpu")


def test_cpu_runtime_loads_asr_and_aligner_without_inspecting_cuda(monkeypatch):
    _cpu_plan(monkeypatch)
    _fake_torch(monkeypatch)
    model_class = Mock()
    model_class.from_pretrained.return_value = object()

    text._load(model_class, "model", "asr")
    text._load(model_class, "model", "aligner")

    assert [call.kwargs["device_map"] for call in model_class.from_pretrained.call_args_list] == [
        "cpu",
        "cpu",
    ]
