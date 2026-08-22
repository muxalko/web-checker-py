"""Safe SQLite backup and retention maintenance."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from web_checker.storage import SQLiteObservationStore, StorageError


@dataclass(frozen=True)
class BackupStatus:
    completed_at: datetime
    path: Path
    bytes: int


def create_backup(database: Path, directory: Path, *, retain: int) -> BackupStatus:
    if retain < 1:
        raise StorageError("backup retention count must be at least 1")
    directory.mkdir(parents=True, exist_ok=True)
    completed_at = datetime.now(UTC)
    destination = directory / f"web-checker-{completed_at:%Y%m%dT%H%M%SZ}.db"
    temporary = destination.with_suffix(".db.tmp")
    try:
        with sqlite3.connect(database) as source, sqlite3.connect(temporary) as target:
            source.backup(target)
            integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise StorageError(f"backup integrity check failed: {integrity}")
        temporary.replace(destination)
        backups = sorted(directory.glob("web-checker-*.db"), reverse=True)
        for expired in backups[retain:]:
            expired.unlink()
        status = BackupStatus(completed_at, destination, destination.stat().st_size)
        (directory / "last-backup.json").write_text(
            json.dumps(
                {
                    "completed_at": completed_at.isoformat(),
                    "path": destination.name,
                    "bytes": status.bytes,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return status
    except (OSError, sqlite3.Error) as error:
        raise StorageError(f"could not create database backup: {error}") from error
    finally:
        temporary.unlink(missing_ok=True)


def read_backup_status(directory: Path) -> BackupStatus | None:
    manifest = directory / "last-backup.json"
    if not manifest.exists():
        return None
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"))
        return BackupStatus(
            datetime.fromisoformat(value["completed_at"]),
            directory / value["path"],
            int(value["bytes"]),
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise StorageError(f"could not read backup status: {error}") from error


def validate_restore(path: Path) -> None:
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as connection:
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise StorageError("restored database failed integrity check")
            SQLiteObservationStore(path).status()
    except sqlite3.Error as error:
        raise StorageError(f"could not validate restored database: {error}") from error
