from unittest.mock import MagicMock, Mock

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

import database
from tests._shared import patch_attrs, raises


def test_sqlite_connection_configuration_always_closes_cursor():
    cursor, connection = Mock(), Mock()
    connection.cursor.return_value = cursor

    database._configure_sqlite(connection, None)

    assert [call.args[0] for call in cursor.execute.call_args_list] == [
        "PRAGMA foreign_keys=ON",
        "PRAGMA journal_mode=WAL",
        "PRAGMA synchronous=NORMAL",
        "PRAGMA busy_timeout=30000",
    ]
    cursor.close.assert_called_once_with()

    cursor.reset_mock()
    cursor.execute.side_effect = RuntimeError("pragma failed")
    raises(RuntimeError, lambda: database._configure_sqlite(connection, None), match='pragma failed')
    cursor.close.assert_called_once_with()


def test_additive_migrations_execute_only_missing_columns():
    connection = Mock()
    database._apply_additive_migrations(
        connection,
        {"existing"},
        {"existing": "ALTER existing", "missing": "ALTER missing"},
    )
    assert str(connection.execute.call_args.args[0]) == "ALTER missing"


def test_audio_datetime_repair_handles_absent_and_incomplete_tables(monkeypatch):
    connection, inspector = Mock(), Mock()
    monkeypatch.setattr(database, "inspect", Mock(return_value=inspector))

    inspector.get_table_names.return_value = []
    database._repair_invalid_audio_settings_datetime(connection)
    connection.execute.assert_not_called()

    inspector.get_table_names.return_value = ["audio_settings"]
    inspector.get_columns.return_value = [{"name": "id"}]
    database._repair_invalid_audio_settings_datetime(connection)
    connection.execute.assert_not_called()

    inspector.get_columns.return_value = [{"name": "updated_at"}]
    database._repair_invalid_audio_settings_datetime(connection)
    assert "UPDATE audio_settings" in str(connection.execute.call_args.args[0])


def test_corrupted_audio_settings_repair_removes_only_invalid_rows():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE audio_settings ("
                "id INTEGER, volume, sensitivity, latency_ms, audio_driver, buffer_size, "
                "monitoring_enabled, reverb, echo, delay)"
            )
        )
        valid = {
            "id": 1,
            "volume": 1.0,
            "sensitivity": 0.5,
            "latency_ms": 50,
            "audio_driver": "auto",
            "buffer_size": 64,
            "monitoring_enabled": 0,
            "reverb": 0.0,
            "echo": 0.0,
            "delay": 0.0,
        }
        columns = ", ".join(valid)
        parameters = ", ".join(f":{name}" for name in valid)
        connection.execute(
            text(f"INSERT INTO audio_settings ({columns}) VALUES ({parameters})"), valid
        )
        connection.execute(
            text(f"INSERT INTO audio_settings ({columns}) VALUES ({parameters})"),
            valid | {"id": 2, "reverb": "broken"},
        )
        database._repair_corrupted_audio_settings(connection)
        assert connection.execute(text("SELECT id FROM audio_settings")).scalars().all() == [1]
    engine.dispose()


def test_corrupted_audio_settings_repair_skips_absent_or_legacy_table(monkeypatch):
    connection, inspector = Mock(), Mock()
    monkeypatch.setattr(database, "inspect", Mock(return_value=inspector))

    inspector.get_table_names.return_value = []
    database._repair_corrupted_audio_settings(connection)
    connection.execute.assert_not_called()

    inspector.get_table_names.return_value = ["audio_settings"]
    inspector.get_columns.return_value = [{"name": "id"}]
    database._repair_corrupted_audio_settings(connection)
    connection.execute.assert_not_called()


def test_interrupted_jobs_are_cancelled():
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE songs (status TEXT, progress_step TEXT, "
                "progress_percent FLOAT, error_message TEXT)"
            )
        )
        connection.execute(
            text("INSERT INTO songs (status) VALUES ('QUEUED'), ('CANCELLING'), ('DONE')")
        )
        database._mark_interrupted_jobs(connection)
        rows = connection.execute(
            text("SELECT status, progress_step, progress_percent FROM songs ORDER BY rowid")
        ).all()
        assert rows == [
            ("CANCELLED", "Interrupted", 0.0),
            ("CANCELLED", "Interrupted", 0.0),
            ("DONE", None, None),
        ]
    engine.dispose()


def test_audio_output_name_migration_adds_the_column_to_an_existing_table():
    # output_device_id is a PortAudio index, not a stable identity -- it can
    # silently start meaning a different device after a USB reconnect. This
    # column lets the output side recover by name the same way
    # input_device_name already lets the input side.
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE audio_settings (id INTEGER, output_device_id INTEGER)"))
        database._apply_audio_output_name_migration(connection)
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(audio_settings)"))}
        assert "output_device_name" in columns

        # Idempotent: running it again against a table that already has the
        # column must not raise "duplicate column name".
        database._apply_audio_output_name_migration(connection)
    engine.dispose()


def test_audio_output_name_migration_skips_an_absent_table(monkeypatch):
    connection, inspector = Mock(), Mock()
    monkeypatch.setattr(database, "inspect", Mock(return_value=inspector))
    inspector.get_table_names.return_value = []

    database._apply_audio_output_name_migration(connection)

    connection.execute.assert_not_called()


def test_init_db_orchestrates_schema_migrations_and_repairs(monkeypatch):
    engine = MagicMock()
    connection = engine.begin.return_value.__enter__.return_value
    patch_attrs(monkeypatch, database, engine=engine)
    create_all = Mock()
    monkeypatch.setattr(database.Base.metadata, "create_all", create_all)
    migrate, datetime_repair, settings_repair, interrupted = Mock(), Mock(), Mock(), Mock()
    patch_attrs(monkeypatch, database, _run_schema_migrations=migrate, _repair_invalid_audio_settings_datetime=datetime_repair, _repair_corrupted_audio_settings=settings_repair, _mark_interrupted_jobs=interrupted)

    database.init_db()

    create_all.assert_called_once_with(bind=engine)
    migrate.assert_called_once_with(connection)
    datetime_repair.assert_called_once_with(connection)
    settings_repair.assert_called_once_with(connection)
    interrupted.assert_called_once_with(connection)


def test_init_db_upgrades_a_real_pre_migration_database_in_place(monkeypatch):
    # A minimal stand-in for a database file saved by an old release: only the
    # columns that existed before every additive migration below was added.
    # This exercises the actual upgrade path end-to-end (not mocked), on a
    # StaticPool sqlite:// engine so the schema persists across connections
    # like a real file would, and checks existing data survives untouched.
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE songs (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "original_filename TEXT NOT NULL, source_path TEXT NOT NULL, "
            "slug TEXT UNIQUE NOT NULL, output_dir TEXT, status TEXT NOT NULL DEFAULT 'PENDING', "
            "progress_step TEXT, progress_percent FLOAT, error_message TEXT, "
            "key_override TEXT, tempo_override FLOAT, note_range_min INTEGER, "
            "note_range_max INTEGER, difficulty_override TEXT, show_lyrics BOOLEAN, "
            "show_notes BOOLEAN, optimized BOOLEAN, created_at DATETIME, updated_at DATETIME)"
        ))
        connection.execute(text(
            "INSERT INTO songs (id, title, original_filename, source_path, slug) "
            "VALUES ('old-song', 'Old Song', 'old.mp3', '/library/old-song/source.mp3', 'old-song')"
        ))
        connection.execute(text(
            "CREATE TABLE recordings (id TEXT PRIMARY KEY, song_id TEXT, filename TEXT NOT NULL, "
            "path TEXT NOT NULL, duration_sec FLOAT, sample_rate INTEGER, created_at DATETIME)"
        ))
        connection.execute(text(
            "INSERT INTO recordings (id, song_id, filename, path) "
            "VALUES ('old-rec', 'old-song', 'take.wav', '/library/old-song/recordings/take.wav')"
        ))

    patch_attrs(monkeypatch, database, engine=engine)
    # Real create_all: it only creates TABLES that don't exist yet (audio_settings,
    # playback_state, ...) and leaves the already-existing songs/recordings tables
    # alone, so this still exercises the additive-column-migration path for them.
    database.init_db()

    with engine.begin() as connection:
        song_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(songs)"))}
        recording_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(recordings)"))}
        assert {"video_url", "artist", "genre", "key_user_edited", "tempo_user_edited"} <= song_columns
        assert {"playback_offset_sec", "playback_segments_json"} <= recording_columns

        song = connection.execute(text("SELECT title, video_url FROM songs WHERE id = 'old-song'")).one()
        assert song == ("Old Song", None)
        recording = connection.execute(
            text(
                "SELECT filename, playback_offset_sec, playback_segments_json "
                "FROM recordings WHERE id = 'old-rec'"
            )
        ).one()
        assert recording == ("take.wav", 0, None)
        history = connection.execute(
            text("SELECT version, name FROM schema_migrations ORDER BY version")
        ).all()
        assert history == [
            (1, "baseline-additive-columns"),
            (2, "history-lookup-indexes"),
            (3, "audio-output-device-name"),
        ]
        # GET /history's join/sort columns (see database._apply_index_migrations)
        # must actually get indexed on an upgraded pre-existing database, not
        # just on a fresh one created through Base.metadata.create_all.
        index_names = {
            row[1] for row in connection.execute(text("PRAGMA index_list(songs)"))
        } | {row[1] for row in connection.execute(text("PRAGMA index_list(recordings)"))}
        assert {
            "ix_songs_created_at", "ix_songs_updated_at",
            "ix_recordings_song_id", "ix_recordings_created_at",
        } <= index_names
    status = database.schema_status()
    assert status["current"] == status["target"] == database.CURRENT_SCHEMA_VERSION
    assert [row["name"] for row in status["history"]] == [
        "baseline-additive-columns", "history-lookup-indexes", "audio-output-device-name"
    ]
    engine.dispose()


def test_versioned_migrations_are_idempotent_and_fail_closed_on_history_mismatch(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    applied = []

    def apply(connection):
        applied.append("run")
        connection.execute(text("CREATE TABLE IF NOT EXISTS migration_marker (id INTEGER)"))

    migration = database.SchemaMigration(1, "fixture", "checksum-a", apply)
    monkeypatch.setattr(database, "_schema_migrations", lambda: (migration,))
    with engine.begin() as connection:
        database._run_schema_migrations(connection)
    with engine.begin() as connection:
        database._run_schema_migrations(connection)
    assert applied == ["run"]

    monkeypatch.setattr(
        database,
        "_schema_migrations",
        lambda: (database.SchemaMigration(1, "fixture", "checksum-b", apply),),
    )
    with engine.begin() as connection:
        raises(RuntimeError, lambda: database._run_schema_migrations(connection), match="mismatch")
    engine.dispose()


def test_interrupted_versioned_migration_can_be_retried(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    attempts = 0

    def apply(connection):
        nonlocal attempts
        attempts += 1
        connection.execute(text("CREATE TABLE IF NOT EXISTS retry_marker (id INTEGER)"))
        if attempts == 1:
            raise RuntimeError("injected migration crash")

    monkeypatch.setattr(
        database,
        "_schema_migrations",
        lambda: (database.SchemaMigration(1, "retry", "checksum", apply),),
    )
    raises(
        RuntimeError,
        lambda: _run_in_transaction(engine, database._run_schema_migrations),
        match="injected",
    )
    _run_in_transaction(engine, database._run_schema_migrations)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version FROM schema_migrations")).scalar_one() == 1
    assert attempts == 2
    engine.dispose()


def _run_in_transaction(engine, action):
    with engine.begin() as connection:
        return action(connection)


def test_database_dependency_closes_session(monkeypatch):
    session = Mock()
    monkeypatch.setattr(database, "SessionLocal", Mock(return_value=session))
    dependency = database.get_db()
    assert next(dependency) is session
    dependency.close()
    session.close.assert_called_once_with()
