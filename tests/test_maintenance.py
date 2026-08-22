import pytest

from web_checker.maintenance import (
    create_backup,
    read_backup_status,
    validate_restore,
)
from web_checker.storage import SQLiteObservationStore, StorageError


def test_backup_is_consistent_and_publishes_status(tmp_path):
    database = tmp_path / "state.db"
    SQLiteObservationStore(database)

    backup = create_backup(database, tmp_path / "backups", retain=2)

    assert backup.path.exists()
    assert read_backup_status(tmp_path / "backups") == backup
    validate_restore(backup.path)


def test_backup_retention_removes_only_expired_database_files(tmp_path):
    database = tmp_path / "state.db"
    SQLiteObservationStore(database)
    backup_directory = tmp_path / "backups"
    backup_directory.mkdir()
    for name in ("web-checker-20260101T000000Z.db", "web-checker-20260102T000000Z.db"):
        (backup_directory / name).write_bytes(b"expired")

    current = create_backup(database, backup_directory, retain=2)

    assert sorted(path.name for path in backup_directory.glob("*.db")) == [
        "web-checker-20260102T000000Z.db",
        current.path.name,
    ]


def test_restore_validation_rejects_corrupt_database(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")

    with pytest.raises(StorageError, match="validate restored"):
        validate_restore(corrupt)
