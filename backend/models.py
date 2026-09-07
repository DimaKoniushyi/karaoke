"""Typed SQLAlchemy models for the local A&D Voice database."""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base


def _new_id() -> str: return uuid.uuid4().hex


def _utc_now() -> datetime: return datetime.now(UTC)


class SongStatus(enum.StrEnum):
    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    CANCELLING = "cancelling"
    DONE = "done"
    CANCELLED = "cancelled"
    ERROR = "error"


class Song(Base):
    __tablename__ = "songs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    title: Mapped[str] = mapped_column(String, nullable=False)
    artist: Mapped[str | None] = mapped_column(String)
    genre: Mapped[str | None] = mapped_column(String)
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    # Path to the original uploaded/imported audio file (pre-processing),
    # distinct from output_dir below (where the processed instrumental/
    # vocals/lyricsSync artifacts are written). Always resolved against the
    # trusted library roots -- see song_service.resolve_source_path().
    source_path: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    output_dir: Mapped[str | None] = mapped_column(String)
    status: Mapped[SongStatus] = mapped_column(Enum(SongStatus), default=SongStatus.PENDING)
    progress_step: Mapped[str | None] = mapped_column(String)
    progress_percent: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text)
    key_override: Mapped[str | None] = mapped_column(String)
    tempo_override: Mapped[float | None] = mapped_column(Float)
    key_user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    tempo_user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    note_range_min: Mapped[int | None] = mapped_column(Integer)
    note_range_max: Mapped[int | None] = mapped_column(Integer)
    difficulty_override: Mapped[str | None] = mapped_column(String)
    video_url: Mapped[str | None] = mapped_column(String)
    show_lyrics: Mapped[bool] = mapped_column(Boolean, default=True)
    show_notes: Mapped[bool] = mapped_column(Boolean, default=True)
    optimized: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now, index=True
    )

    recordings: Mapped[list[Recording]] = relationship(
        back_populates="song", cascade="all, delete-orphan"
    )
    playback_state: Mapped[PlaybackState | None] = relationship(
        back_populates="song", uselist=False, cascade="all, delete-orphan"
    )


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    song_id: Mapped[str] = mapped_column(ForeignKey("songs.id"), index=True)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    duration_sec: Mapped[float | None] = mapped_column(Float)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    playback_offset_sec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    playback_segments_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now, index=True)

    song: Mapped[Song] = relationship(back_populates="recordings")
    analysis: Mapped[AnalysisResult | None] = relationship(
        back_populates="recording", uselist=False, cascade="all, delete-orphan"
    )


class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_new_id)
    recording_id: Mapped[str] = mapped_column(ForeignKey("recordings.id"), unique=True)
    pitch_accuracy_percent: Mapped[float | None] = mapped_column(Float)
    mean_deviation_semitones: Mapped[float | None] = mapped_column(Float)
    rhythm_accuracy_percent: Mapped[float | None] = mapped_column(Float)
    note_hold_percent: Mapped[float | None] = mapped_column(Float)
    note_coverage_percent: Mapped[float | None] = mapped_column(Float)
    overall_score_percent: Mapped[float | None] = mapped_column(Float)
    sections_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utc_now)

    recording: Mapped[Recording] = relationship(back_populates="analysis")


class PlaybackState(Base):
    __tablename__ = "playback_states"

    song_id: Mapped[str] = mapped_column(ForeignKey("songs.id"), primary_key=True)
    position_sec: Mapped[float] = mapped_column(Float, default=0.0)
    is_playing: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )

    song: Mapped[Song] = relationship(back_populates="playback_state")


class AudioSettings(Base):
    __tablename__ = "audio_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    input_device_id: Mapped[int | None] = mapped_column(Integer)
    input_device_name: Mapped[str | None] = mapped_column(String)
    output_device_id: Mapped[int | None] = mapped_column(Integer)
    output_device_name: Mapped[str | None] = mapped_column(String)
    volume: Mapped[float] = mapped_column(Float, default=1.0)
    sensitivity: Mapped[float] = mapped_column(Float, default=0.5)
    latency_ms: Mapped[int] = mapped_column(Integer, default=50)
    audio_driver: Mapped[str] = mapped_column(String, default="auto")
    asio_driver_name: Mapped[str | None] = mapped_column(String)
    buffer_size: Mapped[int] = mapped_column(Integer, default=64)
    monitoring_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    reverb: Mapped[float] = mapped_column(Float, default=0.0)
    echo: Mapped[float] = mapped_column(Float, default=0.0)
    delay: Mapped[float] = mapped_column(Float, default=0.0)
    noise_suppression: Mapped[float] = mapped_column(Float, default=0.35)
    octave: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utc_now, onupdate=_utc_now
    )
