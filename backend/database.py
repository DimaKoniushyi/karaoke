"""
Настройка локальной SQLite-базы через SQLAlchemy.

SQLite выбрана потому, что это десктоп-программа: один файл на диске,
не нужен отдельный процесс сервера БД, работает "из коробки" без установки
чего-либо дополнительного пользователем.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

import config

# check_same_thread=False — нужно, т.к. FastAPI обрабатывает запросы в разных
# потоках (и фоновая обработка песни в pipeline_service тоже работает в
# отдельном потоке). Сама SQLite при этом не потокобезопасна "из коробки" для
# ОДНОГО соединения, поэтому мы открываем новую Session на каждый запрос/
# фоновую задачу через SessionLocal(), а не шарим одно соединение.
engine = create_engine(
    config.DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    """Enable integrity and concurrency settings for every SQLite connection."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
    finally:
        cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    """Shared declarative base for all persisted application models."""


_SONG_COLUMN_MIGRATIONS = {
    "video_url": "ALTER TABLE songs ADD COLUMN video_url VARCHAR",
    "artist": "ALTER TABLE songs ADD COLUMN artist VARCHAR",
    "genre": "ALTER TABLE songs ADD COLUMN genre VARCHAR",
    "key_user_edited": ("ALTER TABLE songs ADD COLUMN key_user_edited BOOLEAN NOT NULL DEFAULT 0"),
    "tempo_user_edited": (
        "ALTER TABLE songs ADD COLUMN tempo_user_edited BOOLEAN NOT NULL DEFAULT 0"
    ),
}

_RECORDING_COLUMN_MIGRATIONS = {
    "playback_offset_sec": (
        "ALTER TABLE recordings ADD COLUMN playback_offset_sec FLOAT NOT NULL DEFAULT 0"
    ),
    "playback_segments_json": "ALTER TABLE recordings ADD COLUMN playback_segments_json TEXT",
}

_ANALYSIS_COLUMN_MIGRATIONS = {
    "rhythm_accuracy_percent": "ALTER TABLE analysis_results ADD COLUMN rhythm_accuracy_percent FLOAT",
    "note_hold_percent": "ALTER TABLE analysis_results ADD COLUMN note_hold_percent FLOAT",
    "note_coverage_percent": "ALTER TABLE analysis_results ADD COLUMN note_coverage_percent FLOAT",
    "overall_score_percent": "ALTER TABLE analysis_results ADD COLUMN overall_score_percent FLOAT",
}

_AUDIO_COLUMN_MIGRATIONS = {
    "audio_driver": ("ALTER TABLE audio_settings ADD COLUMN audio_driver VARCHAR DEFAULT 'auto'"),
    "asio_driver_name": "ALTER TABLE audio_settings ADD COLUMN asio_driver_name VARCHAR",
    "buffer_size": "ALTER TABLE audio_settings ADD COLUMN buffer_size INTEGER DEFAULT 64",
    "output_device_id": "ALTER TABLE audio_settings ADD COLUMN output_device_id INTEGER",
    "reverb": "ALTER TABLE audio_settings ADD COLUMN reverb FLOAT DEFAULT 0",
    "echo": "ALTER TABLE audio_settings ADD COLUMN echo FLOAT DEFAULT 0",
    "delay": "ALTER TABLE audio_settings ADD COLUMN delay FLOAT DEFAULT 0",
    "noise_suppression": (
        "ALTER TABLE audio_settings ADD COLUMN noise_suppression FLOAT DEFAULT 0.35"
    ),
    "octave": "ALTER TABLE audio_settings ADD COLUMN octave FLOAT DEFAULT 0",
}

_AUDIO_OUTPUT_NAME_MIGRATION = {
    "output_device_name": "ALTER TABLE audio_settings ADD COLUMN output_device_name VARCHAR",
}

_INDEX_MIGRATIONS = {
    "ix_songs_created_at": "CREATE INDEX IF NOT EXISTS ix_songs_created_at ON songs (created_at)",
    "ix_songs_updated_at": "CREATE INDEX IF NOT EXISTS ix_songs_updated_at ON songs (updated_at)",
    "ix_recordings_song_id": "CREATE INDEX IF NOT EXISTS ix_recordings_song_id ON recordings (song_id)",
    "ix_recordings_created_at": (
        "CREATE INDEX IF NOT EXISTS ix_recordings_created_at ON recordings (created_at)"
    ),
}

CURRENT_SCHEMA_VERSION = 3


@dataclass(frozen=True)
class SchemaMigration:
    version: int
    name: str
    checksum: str
    apply: Callable[[object], None]


def _migration_checksum(name: str, statements: list[str]) -> str:
    payload = "\n".join([name, *statements]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _apply_additive_migrations(connection, existing: set[str], migrations: dict[str, str]) -> None:
    """Apply missing-column migrations without rebuilding user tables."""
    for column, statement in migrations.items():
        if column not in existing: connection.execute(text(statement))


def _apply_baseline_schema(connection) -> None:
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table, migrations in (
        ("songs", _SONG_COLUMN_MIGRATIONS),
        ("audio_settings", _AUDIO_COLUMN_MIGRATIONS),
        ("recordings", _RECORDING_COLUMN_MIGRATIONS),
        ("analysis_results", _ANALYSIS_COLUMN_MIGRATIONS),
    ):
        if table not in tables:
            continue
        existing = {column["name"] for column in inspect(connection).get_columns(table)}
        _apply_additive_migrations(connection, existing, migrations)


def _apply_audio_output_name_migration(connection) -> None:
    # A saved output_device_id is a PortAudio index, not a stable identity --
    # it can silently start meaning a different device after a USB
    # reconnect/reorder. input_device_name already lets the input side
    # recover its device by name in that case (see
    # audio_service._resolved_device_index); the output side had no
    # equivalent at all.
    inspector = inspect(connection)
    if "audio_settings" not in set(inspector.get_table_names()):
        return
    existing = {column["name"] for column in inspect(connection).get_columns("audio_settings")}
    _apply_additive_migrations(connection, existing, _AUDIO_OUTPUT_NAME_MIGRATION)


def _apply_index_migrations(connection) -> None:
    # GET /history (see application.py) is polled every few seconds and joins
    # Recording<->Song by song_id, sorted by created_at/updated_at -- without
    # these, SQLite has to scan and sort the whole table on every poll.
    # CREATE INDEX IF NOT EXISTS is itself idempotent, but only run it against
    # tables that actually exist (a from-scratch install already gets these
    # indexes from Base.metadata.create_all via models.py's index=True).
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    for table in ("songs", "recordings"):
        if table not in tables:
            continue
        for statement in _INDEX_MIGRATIONS.values():
            if f" ON {table} " in statement:
                connection.execute(text(statement))


def _schema_migrations() -> tuple[SchemaMigration, ...]:
    statements = [
        statement
        for migrations in (
            _SONG_COLUMN_MIGRATIONS,
            _AUDIO_COLUMN_MIGRATIONS,
            _RECORDING_COLUMN_MIGRATIONS,
            _ANALYSIS_COLUMN_MIGRATIONS,
        )
        for statement in migrations.values()
    ]
    name = "baseline-additive-columns"
    index_name = "history-lookup-indexes"
    index_statements = list(_INDEX_MIGRATIONS.values())
    output_name = "audio-output-device-name"
    output_statements = list(_AUDIO_OUTPUT_NAME_MIGRATION.values())
    return (
        SchemaMigration(1, name, _migration_checksum(name, statements), _apply_baseline_schema),
        SchemaMigration(
            2, index_name, _migration_checksum(index_name, index_statements), _apply_index_migrations
        ),
        SchemaMigration(
            3, output_name, _migration_checksum(output_name, output_statements),
            _apply_audio_output_name_migration,
        ),
    )


def _run_schema_migrations(connection) -> None:
    connection.execute(
        text(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
            "applied_at TEXT NOT NULL)"
        )
    )
    applied = {
        row.version: row
        for row in connection.execute(
            text("SELECT version, name, checksum, applied_at FROM schema_migrations")
        ).mappings()
    }
    for migration in _schema_migrations():
        previous = applied.get(migration.version)
        if previous is not None:
            if previous["name"] != migration.name or previous["checksum"] != migration.checksum:
                raise RuntimeError(
                    f"Database migration {migration.version} checksum/history mismatch"
                )
            continue
        migration.apply(connection)
        connection.execute(
            text(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (:version, :name, :checksum, :applied_at)"
            ),
            {
                "version": migration.version,
                "name": migration.name,
                "checksum": migration.checksum,
                "applied_at": datetime.now(UTC).isoformat(),
            },
        )


def schema_status() -> dict[str, object]:
    with engine.connect() as connection:
        if "schema_migrations" not in inspect(connection).get_table_names():
            return {"current": 0, "target": CURRENT_SCHEMA_VERSION, "history": []}
        history = [dict(row) for row in connection.execute(
            text(
                "SELECT version, name, checksum, applied_at FROM schema_migrations "
                "ORDER BY version"
            )
        ).mappings()]
    return {
        "current": max((int(row["version"]) for row in history), default=0),
        "target": CURRENT_SCHEMA_VERSION,
        "history": history,
    }


def _repair_invalid_audio_settings_datetime(connection) -> None:
    """Repair legacy/corrupted SQLite datetime values before ORM reads them."""
    inspector = inspect(connection)
    if "audio_settings" not in inspector.get_table_names(): return
    columns = {column["name"] for column in inspector.get_columns("audio_settings")}
    if "updated_at" not in columns: return
    # SQLAlchemy's SQLite DateTime processor uses datetime.fromisoformat().
    # SQLite datetime(value) returns NULL for malformed values, letting us
    # repair only rows that the ORM would otherwise fail to deserialize.
    connection.execute(
        text(
            "UPDATE audio_settings "
            "SET updated_at = strftime('%Y-%m-%d %H:%M:%f', 'now') "
            "WHERE updated_at IS NULL "
            "OR typeof(updated_at) != 'text' "
            "OR datetime(updated_at) IS NULL"
        )
    )


def _repair_corrupted_audio_settings(connection) -> None:
    """Reset only the audio settings row when legacy values no longer fit the schema.

    Older application builds changed the AudioSettings layout several times. SQLite is
    dynamically typed, so a legacy row can survive migrations with values such as an
    ASIO driver name stored in a numeric effects column.  SQLAlchemy then either fails
    while deserializing the row or FastAPI rejects the response.  Audio settings are
    user preferences, not library data, so the safest recovery is to discard only the
    corrupted settings row and let the ORM recreate it with current defaults.
    """
    inspector = inspect(connection)
    if "audio_settings" not in inspector.get_table_names(): return
    columns, required = {column['name'] for column in inspector.get_columns('audio_settings')}, {'id', 'volume', 'sensitivity', 'latency_ms', 'audio_driver', 'buffer_size', 'monitoring_enabled', 'reverb', 'echo', 'delay'}
    if not required.issubset(columns): return

    rows = (
        connection.execute(
            text(
                "SELECT id, volume, sensitivity, latency_ms, audio_driver, buffer_size, "
                "monitoring_enabled, reverb, echo, delay, "
                + ("noise_suppression" if "noise_suppression" in columns else "0.35 AS noise_suppression")
                + ", "
                + ("octave" if "octave" in columns else "0 AS octave")
                + " FROM audio_settings"
            )
        )
        .mappings()
        .all()
    )

    def is_number(value) -> bool: return isinstance(value, (int, float)) and (not isinstance(value, bool))

    def in_range(value, low: float, high: float) -> bool: return is_number(value) and low <= float(value) <= high

    corrupted_ids: list[int] = []
    for row in rows:
        valid = (
            isinstance(row["id"], int)
            and in_range(row["volume"], 0.0, 4.0)
            and in_range(row["sensitivity"], 0.0, 1.0)
            and isinstance(row["latency_ms"], int)
            and row["latency_ms"] >= 0
            and isinstance(row["audio_driver"], str)
            and row["audio_driver"] in {"auto", "asio"}
            and isinstance(row["buffer_size"], int)
            and 16 <= row["buffer_size"] <= 2048
            and isinstance(row["monitoring_enabled"], int)
            and row["monitoring_enabled"] in {0, 1}
            and in_range(row["reverb"], 0.0, 1.0)
            and in_range(row["echo"], 0.0, 1.0)
            and in_range(row["delay"], 0.0, 1.0)
            and in_range(row["noise_suppression"], 0.0, 1.0)
            and in_range(row["octave"], -1.0, 1.0)
        )
        if not valid: corrupted_ids.append(int(row["id"]))

    for settings_id in corrupted_ids:
        connection.execute(
            text("DELETE FROM audio_settings WHERE id = :settings_id"),
            {"settings_id": settings_id},
        )


def _mark_interrupted_jobs(connection) -> None:
    connection.execute(
        text(
            "UPDATE songs SET status = :cancelled, progress_step = :step, "
            "progress_percent = 0, error_message = :message "
            "WHERE status IN (:queued, :processing, :cancelling)"
        ),
        {
            "cancelled": "CANCELLED",
            "queued": "QUEUED",
            "processing": "PROCESSING",
            "cancelling": "CANCELLING",
            "step": "Interrupted",
            "message": "Processing was interrupted by an application restart",
        },
    )


def init_db() -> None:
    """Create tables and apply backward-compatible SQLite migrations."""
    import models  # noqa: F401  (registers models before create_all)

    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        _run_schema_migrations(connection)
        _repair_invalid_audio_settings_datetime(connection)
        _repair_corrupted_audio_settings(connection)
        _mark_interrupted_jobs(connection)


def get_db():
    """FastAPI-зависимость: одна Session на запрос, всегда закрывается."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
