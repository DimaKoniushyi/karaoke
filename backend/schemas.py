"""
Pydantic-схемы: тела запросов/ответов API.

Отдельно от models.py (ORM) сознательно — наружу мы не всегда хотим отдавать
ровно то, что лежит в базе (например source_path — внутренний путь на диске,
наружу отдаём только то, что реально нужно фронтенду).
"""

from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from models import SongStatus

# --------------------------------------------------------------------
# Песни
# --------------------------------------------------------------------


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProcessingState(BaseModel):
    status: SongStatus
    progress_step: str | None = None
    progress_percent: float
    error_message: str | None = None


class ProcessingRequest(BaseModel):
    mode: Literal["auto", "fast", "quality"] = "auto"


class SongOut(ORMModel, ProcessingState):

    id: str
    title: str
    artist: str | None = None
    genre: str | None = None
    original_filename: str
    slug: str
    output_dir: str | None = None

    key_override: str | None = None
    tempo_override: float | None = None
    note_range_min: int | None = None
    note_range_max: int | None = None
    difficulty_override: str | None = None
    video_url: str | None = None
    show_lyrics: bool
    show_notes: bool
    optimized: bool

    created_at: datetime
    updated_at: datetime


class SongIdentityOut(BaseModel):
    title: str
    artist: str | None = None
    cover_data_url: str | None = None


class KarDatasetItemOut(BaseModel):
    filename: str
    status: Literal["ready", "review", "skipped", "error"]
    dataset_dir: str | None = None
    title: str | None = None
    artist: str | None = None
    word_count: int = 0
    note_count: int = 0
    preparation_mode: str | None = None
    stems_status: str | None = None
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class KarDatasetBatchOut(BaseModel):
    output_root: str
    items: list[KarDatasetItemOut]


class SongUpdate(BaseModel):
    """Все поля опциональны — PATCH-семантика, меняем только переданное."""

    title: str | None = Field(default=None, min_length=1, max_length=255)
    artist: str | None = Field(default=None, max_length=255)
    genre: str | None = Field(default=None, max_length=255)
    key_override: str | None = None
    tempo_override: float | None = Field(default=None, gt=0)
    note_range_min: int | None = Field(default=None, ge=0, le=127)
    note_range_max: int | None = Field(default=None, ge=0, le=127)
    difficulty_override: str | None = None
    video_url: str | None = Field(default=None, max_length=2048)
    show_lyrics: bool | None = None
    show_notes: bool | None = None

    @field_validator("title", "artist", "genre", "key_override", "difficulty_override", "video_url")
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        if value is None: return None
        value = value.strip()
        if not value: raise ValueError("Value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_note_range(self) -> "SongUpdate":
        if (
            self.note_range_min is not None
            and self.note_range_max is not None
            and self.note_range_min > self.note_range_max
        ):
            raise ValueError("note_range_min must not exceed note_range_max")
        return self


class TimedText(BaseModel):
    _time_label: ClassVar[str]
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_time_range(self):
        if self.end < self.start: raise ValueError(f"{self._time_label} end time must not be earlier than its start time")
        return self


class LyricWord(TimedText):
    _time_label = "Word"
    word: str = Field(max_length=512)


class LyricLine(TimedText):
    _time_label = "Line"
    text: str = Field(max_length=4_000)
    words: list[LyricWord] = Field(default_factory=list, max_length=1_000)


class LyricsUpdate(BaseModel):
    lyrics: list[LyricLine] = Field(max_length=10_000)


class ProcessingStatusOut(BaseModel):
    song_id: str
    status: SongStatus
    progress_step: str | None = None
    progress_percent: float
    progress_detail: str | None = None
    eta_seconds: int | None = None
    stage: str | None = None
    stage_elapsed_seconds: int | None = None
    total_elapsed_seconds: int | None = None
    estimated_finish_at: str | None = None
    error_message: str | None = None


class SongRevisionsRequest(BaseModel):
    song_ids: list[str] = Field(min_length=1, max_length=500)


class SongRevisionResult(BaseModel):
    song_id: str
    revision: str | None = None
    error: str | None = None


class SongRevisionsOut(BaseModel):
    revisions: list[SongRevisionResult]


class SongRevisionResolveRequest(BaseModel):
    revision: str = Field(min_length=71, max_length=71, pattern=r"^sha256:[0-9a-f]{64}$")


class SongRevisionResolveOut(BaseModel):
    song_id: str | None = None


class SongEditorUpdate(BaseModel):
    notes: list[dict[str, Any]] = Field(default_factory=list, max_length=20_000)
    word_texts: list[str] | None = Field(default=None, max_length=20_000)
    word_bounds: list[dict[str, float]] | None = Field(default=None, max_length=20_000)


class SongEditorOut(BaseModel):
    lyrics_sync: dict[str, Any]
    ai_backup_exists: bool = False


class SongResultOut(BaseModel):
    """Агрегированные результаты AI-пайплайна по одной песне (содержимое Song/<slug>/*.json)."""

    song: SongOut
    lyrics_sync: Any | None = None


# --------------------------------------------------------------------
# Плеер
# --------------------------------------------------------------------


class PlaybackStateOut(ORMModel):

    song_id: str
    position_sec: float
    is_playing: bool


class SeekRequest(BaseModel):
    position_sec: float = Field(ge=0)


# --------------------------------------------------------------------
# Записи голоса
# --------------------------------------------------------------------


class RecordingOut(ORMModel):

    id: str
    song_id: str
    filename: str
    duration_sec: float | None = None
    sample_rate: int | None = None
    playback_offset_sec: float = 0
    created_at: datetime

    @field_validator("playback_offset_sec", mode="before")
    @classmethod
    def _default_playback_offset(cls, value: object) -> object:
        # An unflushed ORM Recording (constructed without this column set
        # explicitly, e.g. in tests) reports None rather than the column's
        # SQL-side default until it's committed.
        return 0 if value is None else value


class RecordedSongOut(RecordingOut):
    song_title: str


class RecordingStartRequest(BaseModel):
    song_id: str
    position_sec: float = Field(default=0, ge=0)
    music_volume: float = Field(default=1, ge=0, le=1)
    microphone_volume: float = Field(default=1, ge=0, le=4)
    reverb: float = Field(default=0, ge=0, le=1)
    echo: float = Field(default=0, ge=0, le=1)
    delay: float = Field(default=0, ge=0, le=1)
    octave: float = Field(default=0, ge=-1, le=1)
    playback_rate: float = Field(default=1, ge=0.5, le=1.5)
    room_mode: bool = False
    # Set when the frontend successfully connected to the Python monitor's
    # audio relay (see app/routers/audio_relay.py) and intends to use it as
    # the room's DSP source instead of its own local Web Audio graph. Only
    # meaningful when room_mode is also True.
    voice_relay: bool = False


class RecordingControlsRequest(BaseModel):
    music_volume: float = Field(ge=0, le=1)
    microphone_volume: float = Field(ge=0, le=4)
    reverb: float = Field(ge=0, le=1)
    echo: float = Field(ge=0, le=1)
    delay: float = Field(ge=0, le=1)
    octave: float = Field(ge=-1, le=1)


class RecordingStartOut(BaseModel):
    recording_session_id: str
    message: str


# --------------------------------------------------------------------
# Анализ голоса
# --------------------------------------------------------------------


class AnalysisOut(ORMModel):

    id: str
    recording_id: str
    pitch_accuracy_percent: float | None = None
    mean_deviation_semitones: float | None = None
    rhythm_accuracy_percent: float | None = None
    note_hold_percent: float | None = None
    note_coverage_percent: float | None = None
    overall_score_percent: float | None = None
    sections: list[dict[str, Any]] | None = None
    created_at: datetime


# --------------------------------------------------------------------
# Кэш/производительность
# --------------------------------------------------------------------


class CacheSizeOut(BaseModel):
    total_bytes: int
    total_human: str
    breakdown: dict[str, int]
    library_roots: list[dict[str, str | int]] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)


class FreeSpaceOut(BaseModel):
    free_bytes: int
    reserved_bytes: int = 0
    available_bytes: int = 0
    free_human: str
    total_bytes: int
    total_human: str


class OptimizeResultOut(BaseModel):
    song_id: str
    freed_bytes: int
    freed_human: str
    actions: list[str]


# --------------------------------------------------------------------
# Диагностика
# --------------------------------------------------------------------


class HealthOut(BaseModel):
    status: str
    version: str
    build_id: str
    startup: dict[str, object] | None = None


class PipelineHealthOut(BaseModel):
    ffmpeg_available: bool
    demucs_available: bool
    whisper_available: bool
    torch_available: bool
    cuda_available: bool
    ai_dir_found: bool
    runtime: dict[str, object] | None = None


class AIModelResourceOut(BaseModel):
    key: str
    name: str
    ready: bool


class AIModelsStatusOut(BaseModel):
    state: str
    ready: bool
    ready_count: int
    total: int
    current_model: str | None = None
    error: str | None = None
    models_dir: str
    models: list[AIModelResourceOut]
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    remaining_seconds: int | None = None


class VersionsOut(BaseModel):
    backend_version: str
    build_id: str
    python_version: str
    components: dict[str, str | None]


class SystemErrorsOut(BaseModel):
    errors: list[dict[str, Any]]


class ClientLogIn(BaseModel):
    source: str = "renderer"
    level: str = "info"
    message: str
    stack: str | None = None
    url: str | None = None
    user: str | None = None


# --------------------------------------------------------------------
# Аудио-устройства
# --------------------------------------------------------------------


class AudioDeviceBase(BaseModel):
    index: int
    name: str
    default_samplerate: float | None = None
    host_api: str
    is_asio: bool


class AudioDeviceOut(AudioDeviceBase):
    max_input_channels: int


class AudioOutputDeviceOut(AudioDeviceBase):
    max_output_channels: int


class AsioDriverOut(BaseModel):
    name: str


class AudioSettingsOut(ORMModel):

    input_device_id: int | None = None
    input_device_name: str | None = None
    output_device_id: int | None = None
    output_device_name: str | None = None
    volume: float
    sensitivity: float
    latency_ms: int
    audio_driver: str
    asio_driver_name: str | None = None
    buffer_size: int
    monitoring_enabled: bool
    reverb: float
    echo: float
    delay: float
    noise_suppression: float
    octave: float


class AudioSettingsUpdate(BaseModel):
    input_device_id: int | None = None
    output_device_id: int | None = None
    volume: float | None = Field(default=None, ge=0, le=4)
    sensitivity: float | None = Field(default=None, ge=0, le=1)
    latency_ms: int | None = Field(default=None, ge=0)
    audio_driver: str | None = None
    asio_driver_name: str | None = Field(default=None, max_length=255)
    buffer_size: int | None = Field(default=None, ge=16, le=2048)
    monitoring_enabled: bool | None = None
    reverb: float | None = Field(default=None, ge=0, le=1)
    echo: float | None = Field(default=None, ge=0, le=1)
    delay: float | None = Field(default=None, ge=0, le=1)
    noise_suppression: float | None = Field(default=None, ge=0, le=1)
    octave: float | None = Field(default=None, ge=-1, le=1)


class SignalQualityOut(BaseModel):
    rms_db: float
    clipping: bool
    silent: bool
