import contextlib
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import soundfile as sf
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import models
from app.services import song_package_service
from database import Base
from tests._shared import make_song, patch_attrs


def write_audio(path, *, format="FLAC"):
    import numpy as np

    sf.write(path, np.zeros(800, dtype=np.float32), 8_000, format=format)


def write_runtime(output):
    output.mkdir(parents=True, exist_ok=True)
    write_audio(output / "instrumental.flac")
    write_audio(output / "vocals.flac")
    (output / "lyricsSync.json").write_text(
        json.dumps(
            {
                "bpm": 120,
                "key": "Am",
                "words": [
                    {
                        "text": "la",
                        "start": 0.0,
                        "end": 0.1,
                        "notes": [{"note": 60, "start": 0.0, "end": 0.1}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def song(output):
    return make_song(
        id="song-id",
        artist="Artist",
        source_path=str(output / "instrumental.flac"),
        output_dir=str(output),
        status=models.SongStatus.DONE,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_package_contains_only_strict_runtime_files(monkeypatch, tmp_path):
    output = tmp_path / "song"
    write_runtime(output)
    (output / "private.json").write_text("{}", encoding="utf-8")
    patch_attrs(monkeypatch, song_package_service.config, CACHE_DIR=tmp_path, SONG_OUTPUT_DIR=tmp_path)

    package = song_package_service.build_package(song(output))
    try:
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            assert {name for name in names if name.startswith("output/")} == {
                "output/instrumental.flac",
                "output/vocals.flac",
                "output/lyricsSync.json",
            }
            # source_path already equals output/instrumental.flac (post-processing
            # state) — storing it a second time under source/ would just double
            # the archive size for a file import discards anyway.
            assert not {name for name in names if name.startswith("source/")}
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["content_revision"].startswith("sha256:")
    finally:
        package.unlink()


def test_build_package_hashes_artifacts_only_once(monkeypatch, tmp_path):
    # build_package() used to call compute_content_revision() directly for its
    # own pre-check, bypassing the signature cache, and then _manifest() asked
    # for the same revision again -- two full hash passes over every artifact
    # for one export. It must now go through the cache so the second ask is
    # a cache hit.
    from unittest.mock import Mock

    output = tmp_path / "song"
    write_runtime(output)
    patch_attrs(monkeypatch, song_package_service.config, CACHE_DIR=tmp_path, SONG_OUTPUT_DIR=tmp_path)

    hashing = Mock(wraps=song_package_service.compute_content_revision)
    monkeypatch.setattr(song_package_service, "compute_content_revision", hashing)

    package = song_package_service.build_package(song(output))
    try:
        assert hashing.call_count == 1
    finally:
        package.unlink()


def test_package_preserves_a_source_distinct_from_the_instrumental(monkeypatch, tmp_path):
    output = tmp_path / "song"
    write_runtime(output)
    original = tmp_path / "song" / "source.wav"
    write_audio(original, format="WAV")
    patch_attrs(monkeypatch, song_package_service.config, CACHE_DIR=tmp_path, SONG_OUTPUT_DIR=tmp_path)

    current = song(output)
    current.source_path = str(original)
    package = song_package_service.build_package(current)
    try:
        with zipfile.ZipFile(package) as archive:
            names = set(archive.namelist())
            assert {name for name in names if name.startswith("source/")} == {"source/source.wav"}
    finally:
        package.unlink()


def test_package_export_import_round_trip_preserves_revision(monkeypatch, tmp_path):
    cache_root = tmp_path / "cache-volume"
    cache_root.mkdir()
    export_output = tmp_path / "exporter-library" / "song"
    write_runtime(export_output)
    patch_attrs(
        monkeypatch, song_package_service.config,
        CACHE_DIR=cache_root, SONG_OUTPUT_DIR=export_output.parent, SONG_LIBRARY_ROOTS={export_output.parent},
    )
    exported_song = song(export_output)
    package_path = song_package_service.build_package(exported_song)
    exported_revision = song_package_service.compute_content_revision(exported_song)

    import_root = tmp_path / "importer-library"
    import_root.mkdir()
    patch_attrs(
        monkeypatch, song_package_service.config,
        SONG_OUTPUT_DIR=import_root, SONG_LIBRARY_ROOTS={import_root},
    )
    real_replace = Path.replace

    def reject_cache_to_library_rename(source, target):
        source_path, target_path = source.resolve(), Path(target).resolve()
        if source_path.is_relative_to(cache_root.resolve()) and target_path.is_relative_to(
            import_root.resolve()
        ):
            raise OSError(17, "Cannot move a file to a different disk")
        return real_replace(source, target)

    monkeypatch.setattr(Path, "replace", reject_cache_to_library_rename)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        imported = song_package_service.import_package(db, package_path)
        assert song_package_service.compute_content_revision(imported) == exported_revision
        # Import must materialize the instrumental exactly once (not a second
        # "source" copy) even though it aliases source_path to it.
        assert {path.name for path in (import_root / imported.slug).iterdir()} == {
            "instrumental.flac", "vocals.flac", "lyricsSync.json",
        }
    finally:
        db.close()
        engine.dispose()
        package_path.unlink(missing_ok=True)


def test_package_export_import_round_trip_preserves_a_distinct_source(monkeypatch, tmp_path):
    # Regression test: importing a package whose source differs from
    # instrumental.flac (see test_package_preserves_a_source_distinct_from_
    # the_instrumental for the export side) used to always fail. Import
    # hardcoded final_source_name to "instrumental.flac" and discarded the
    # archive's source/ entry entirely (_prepare_publish_tree never wrote
    # it to disk), so the imported song's source_path pointed at
    # instrumental.flac -- a different file than the one manifest.
    # content_revision was actually hashed from at export time -- and the
    # post-publish revision check in _publish_imported_package always raised.
    cache_root = tmp_path / "cache-volume"
    cache_root.mkdir()
    export_output = tmp_path / "exporter-library" / "song"
    write_runtime(export_output)
    original = export_output / "source.wav"
    write_audio(original, format="WAV")
    patch_attrs(
        monkeypatch, song_package_service.config,
        CACHE_DIR=cache_root, SONG_OUTPUT_DIR=export_output.parent, SONG_LIBRARY_ROOTS={export_output.parent},
    )
    exported_song = song(export_output)
    exported_song.source_path = str(original)
    package_path = song_package_service.build_package(exported_song)
    exported_revision = song_package_service.compute_content_revision(exported_song)

    import_root = tmp_path / "importer-library"
    import_root.mkdir()
    patch_attrs(
        monkeypatch, song_package_service.config,
        SONG_OUTPUT_DIR=import_root, SONG_LIBRARY_ROOTS={import_root},
    )
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        imported = song_package_service.import_package(db, package_path)
        assert song_package_service.compute_content_revision(imported) == exported_revision
        assert Path(imported.source_path).name == "source.wav"
        assert Path(imported.source_path).is_file()
        assert {path.name for path in (import_root / imported.slug).iterdir()} == {
            "instrumental.flac", "vocals.flac", "lyricsSync.json", "source.wav",
        }
    finally:
        db.close()
        engine.dispose()
        package_path.unlink(missing_ok=True)


def test_recover_import_journal_cleans_up_the_staging_directory(monkeypatch, tmp_path):
    # Regression test: a crash between staging_root being populated (in
    # _publish_imported_package) and its own except/finally removing it used
    # to leave that directory -- a full copy of the imported output tree --
    # with no reference anywhere, accumulating on disk forever. The journal
    # now records it and _recover_import_journal removes it regardless of
    # which phase/branch below actually runs (the "prepared" phase here is
    # the simplest one: no DB song lookup needed).
    library_root = tmp_path / "library"
    library_root.mkdir()
    patch_attrs(monkeypatch, song_package_service.config, SONG_OUTPUT_DIR=library_root, SONG_LIBRARY_ROOTS={library_root})

    staging_dir = library_root / ".song.room-import-abc123"
    staging_dir.mkdir()
    (staging_dir / "marker.txt").write_text("scratch", encoding="utf-8")

    journal_path = library_root / ".room-import-journal-test.json"
    song_package_service.write_json(journal_path, {
        "operation": "room-import",
        "song_id": "song",
        "library_root": str(library_root),
        "target": "song",
        "backup": None,
        "staging": staging_dir.name,
        "new_revision": "sha256:" + "0" * 64,
        "phase": "prepared",
    })

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        song_package_service._recover_import_journal(db, journal_path)
    finally:
        db.close()
        engine.dispose()

    assert not staging_dir.exists()
    assert not journal_path.exists()


def test_package_rejects_missing_runtime_file(monkeypatch, tmp_path):
    output = tmp_path / "song"
    write_runtime(output)
    (output / "vocals.flac").unlink()
    patch_attrs(monkeypatch, song_package_service.config, CACHE_DIR=tmp_path, SONG_OUTPUT_DIR=tmp_path)

    try:
        song_package_service.build_package(song(output))
    except ValueError:
        pass
    else:
        raise AssertionError("incomplete runtime package must be rejected")


def test_content_revisions_for_songs_holds_the_library_lock_once_for_the_whole_batch(monkeypatch):
    lock_acquisitions = []

    @contextlib.contextmanager
    def counting_lock():
        lock_acquisitions.append(1)
        yield

    def fake_content_revision_for_song(_db, song_id):
        if song_id == "missing":
            raise ValueError("Song not found")
        return f"sha256:{song_id}"

    patch_attrs(monkeypatch, song_package_service.song_service, library_write_lock=counting_lock)
    patch_attrs(
        monkeypatch, song_package_service,
        content_revision_for_song=fake_content_revision_for_song,
    )

    results = song_package_service.content_revisions_for_songs(None, ["a", "missing", "b"])

    assert results == [
        ("a", "sha256:a", None),
        ("missing", None, "Song not found"),
        ("b", "sha256:b", None),
    ]
    # One lock acquisition for the whole batch, not one per song -- that's
    # the entire point of batching instead of calling this once per song.
    assert lock_acquisitions == [1]


def test_find_song_id_by_content_revision_matches_completed_local_copy(monkeypatch):
    songs = [Mock(id="pending"), Mock(id="local-copy"), Mock(id="other")]
    patch_attrs(
        monkeypatch,
        song_package_service.song_service,
        list_songs=Mock(return_value=songs),
        is_done=Mock(side_effect=lambda item: item.id != "pending"),
    )
    revisions = {"local-copy": "sha256:" + "a" * 64, "other": "sha256:" + "b" * 64}
    patch_attrs(
        monkeypatch,
        song_package_service,
        content_revision_for_song=Mock(side_effect=lambda _db, song_id: revisions[song_id]),
    )

    result = song_package_service.find_song_id_by_content_revision(
        None, "sha256:" + "a" * 64
    )

    assert result == "local-copy"
    # Stops at the first match instead of fingerprinting every completed
    # song -- "other" is never even looked at.
    song_package_service.content_revision_for_song.assert_called_once_with(None, "local-copy")


def test_member_path_rejects_traversal():
    member = zipfile.ZipInfo("../escape")
    try:
        song_package_service._member_path(member)
    except ValueError:
        pass
    else:
        raise AssertionError("unsafe archive path must be rejected")
