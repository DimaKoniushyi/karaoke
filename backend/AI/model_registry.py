from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

ModelKind = Literal["snapshot", "file", "bundle"]


@dataclass(frozen=True, slots=True)
class ModelFile:
    relative_path: str
    url: str
    sha256: str | None = None
    expected_bytes: int = 0
    min_bytes: int = 1
    contains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    name: str
    repo_id: str
    revision: str
    relative_path: str
    env_var: str
    expected_bytes: int = 0
    kind: ModelKind = "snapshot"
    filename: str | None = None
    sha256: str | None = None
    ignore_patterns: tuple[str, ...] = ()
    files: tuple[ModelFile, ...] = ()
    license: str = ""
    license_url: str = ""

    @property
    def local_name(self) -> str:
        return Path(self.relative_path).name


MODELS = (
    ModelSpec("asr", "Qwen3 ASR", "Qwen/Qwen3-ASR-0.6B", "5eb144179a02acc5e5ba31e748d22b0cf3e303b0", "qwen/Qwen3-ASR-0.6B", "KARAOKE_AI_ASR_MODEL", 1_876_091_704, filename="model.safetensors", sha256="79d6cbd4c98c7bbffe9db2edac07f56cd6637d0d5944b27f6c2b8353840323ea", license="Apache-2.0", license_url="https://huggingface.co/Qwen/Qwen3-ASR-0.6B/tree/5eb144179a02acc5e5ba31e748d22b0cf3e303b0"),
    ModelSpec("aligner", "Qwen3 Forced Aligner", "Qwen/Qwen3-ForcedAligner-0.6B", "c7cbfc2048c462b0d63a45797104fc9db3ad62b7", "qwen/Qwen3-ForcedAligner-0.6B", "KARAOKE_AI_ALIGNER_MODEL", 1_840_072_459, filename="model.safetensors", sha256="47831d0e82f96b20e9034dba01a075ee06436654719f6a68289e49f1b65ce0e7", license="Apache-2.0", license_url="https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B/tree/c7cbfc2048c462b0d63a45797104fc9db3ad62b7"),
    ModelSpec("ctc_ru", "Russian CTC aligner", "jonatasgrosman/wav2vec2-large-xlsr-53-russian", "2329100508896c6d9b157019803ab5601e6f3406", "ctc/wav2vec2-large-xlsr-53-russian", "KARAOKE_AI_CTC_RU_MODEL", 1_265_908_849, filename="pytorch_model.bin", sha256="d1cdb1a7921de7d363f967a9b0101a713602e109dba62b6f3f9ae2e0b2df0c1c", ignore_patterns=("language_model/**", "flax_model.msgpack"), license="Apache-2.0", license_url="https://huggingface.co/jonatasgrosman/wav2vec2-large-xlsr-53-russian/tree/2329100508896c6d9b157019803ab5601e6f3406"),
    ModelSpec("ctc_uk", "Ukrainian CTC aligner", "Yehor/wav2vec2-xls-r-300m-uk-with-small-lm", "e3ced4def0d70be3aab0f2db598a59961fe9ab3b", "ctc/wav2vec2-xls-r-300m-uk", "KARAOKE_AI_CTC_UK_MODEL", 1_261_978_306, filename="model.safetensors", sha256="e8d3ea3825853c6016539d474878fc281cf0bbb2fa9209f34f403b91e2eb0fe9", ignore_patterns=("language_model/**", "flax_model.msgpack"), license="Apache-2.0", license_url="https://huggingface.co/Yehor/wav2vec2-xls-r-300m-uk-with-small-lm/tree/e3ced4def0d70be3aab0f2db598a59961fe9ab3b"),
    ModelSpec("roformer", "Mel-Band RoFormer", "KimberleyJSN/melbandroformer", "ac9b0614ab3cd7f77219e18ba494dfd93956c348", "roformer", "MSST_CHECKPOINT", 913_106_900, kind="file", filename="MelBandRoformer.ckpt", sha256="87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e", license="MIT", license_url="https://huggingface.co/KimberleyJSN/melbandroformer/tree/ac9b0614ab3cd7f77219e18ba494dfd93956c348"),
    ModelSpec("vocalparse", "VocalParse symbolic score", "pymaster/VocalParse", "4c617b1a88c8e663351d9072c549d81d7f78a36f", "vocalparse", "KARAOKE_AI_VOCALPARSE_MODEL", 4_076_867_480, filename="model.safetensors", sha256="08a69f96082ed962950b7a6e90cd1482e87b132cecab1d805a7a024fcee7b08d", license="Apache-2.0", license_url="https://huggingface.co/pymaster/VocalParse/tree/4c617b1a88c8e663351d9072c549d81d7f78a36f"),
)
MODEL_BY_KEY = {model.key: model for model in MODELS}


def get_model(key: str) -> ModelSpec:
    return MODEL_BY_KEY[key]


def model_directory(models_root: Path, model: ModelSpec) -> Path:
    return Path(models_root) / model.relative_path


def model_path(models_root: Path, model: ModelSpec) -> Path:
    root = model_directory(models_root, model)
    return root / model.filename if model.kind == "file" and model.filename else root
