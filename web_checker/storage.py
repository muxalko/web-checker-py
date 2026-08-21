"""Transactional SQLite persistence for successful check snapshots."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from web_checker.core.models import Availability, CheckResult, Opportunity
from web_checker.core.service import RecordOutcome
from web_checker.core.transitions import (
    TransitionType,
    detect_initial_transitions,
    detect_transitions,
)
from web_checker.notifications.models import NotificationPlan, PendingNotification

SCHEMA_VERSION = 2


@dataclass(frozen=True)
class RetentionResult:
    check_runs_deleted: int
    completed_notifications_deleted: int


@dataclass(frozen=True)
class StorageStatus:
    database_bytes: int
    oldest_observation: datetime | None
    check_runs: int
    pending_notifications: int


class StorageError(Exception):
    """Raised when persistent checker state cannot be read or written safely."""


class SQLiteObservationStore:
    """Stores immutable check history and normalized transition records."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._initialize()

    def record_success(
        self,
        job_id: str,
        result: CheckResult,
        notification_plan: NotificationPlan | None = None,
    ) -> RecordOutcome:
        plan = notification_plan or NotificationPlan()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            previous = self._get_latest(connection, job_id)
            transitions = (
                detect_initial_transitions(result)
                if previous is None
                else detect_transitions(previous, result)
            )
            cursor = connection.execute(
                "INSERT INTO check_runs (job_id, checked_at) VALUES (?, ?)",
                (job_id, result.checked_at.isoformat()),
            )
            run_id = cursor.lastrowid
            for position, opportunity in enumerate(result.opportunities):
                connection.execute(
                    """
                    INSERT INTO opportunity_observations (
                        run_id, position, opportunity_id, title, availability,
                        starts_at, booking_url, attributes_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        position,
                        opportunity.id,
                        opportunity.title,
                        opportunity.availability.value,
                        opportunity.starts_at.isoformat()
                        if opportunity.starts_at is not None
                        else None,
                        opportunity.booking_url,
                        json.dumps(
                            dict(opportunity.attributes),
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )
            for transition in transitions:
                transition_cursor = connection.execute(
                    """
                    INSERT INTO transitions (
                        run_id, opportunity_id, transition_type,
                        previous_availability, current_availability
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        transition.opportunity_id,
                        transition.type.value,
                        transition.previous.availability.value
                        if transition.previous is not None
                        else None,
                        transition.current.availability.value
                        if transition.current is not None
                        else None,
                    ),
                )
                if transition.type in plan.on:
                    opportunity = transition.current or transition.previous
                    for channel in plan.channels:
                        connection.execute(
                            """
                            INSERT INTO notification_outbox (
                                transition_id, channel, job_id, transition_type,
                                opportunity_id, opportunity_title,
                                current_availability, booking_url, checked_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                transition_cursor.lastrowid,
                                channel,
                                job_id,
                                transition.type.value,
                                transition.opportunity_id,
                                opportunity.title,
                                transition.current.availability.value
                                if transition.current is not None
                                else None,
                                opportunity.booking_url,
                                result.checked_at.isoformat(),
                            ),
                        )
            connection.commit()
            return RecordOutcome(
                baseline_created=previous is None,
                transitions=transitions,
            )
        except (sqlite3.Error, TypeError, ValueError) as error:
            connection.rollback()
            raise StorageError(
                f"could not record successful check for job {job_id!r}: {error}"
            ) from error
        finally:
            connection.close()

    def get_latest(self, job_id: str) -> CheckResult | None:
        connection = self._connect()
        try:
            return self._get_latest(connection, job_id)
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise StorageError(
                f"could not read latest check for job {job_id!r}: {error}"
            ) from error
        finally:
            connection.close()

    def prune(
        self, *, observations_before: datetime, notifications_before: datetime
    ) -> RetentionResult:
        """Delete expired history without losing baselines or pending deliveries."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            completed = connection.execute(
                """
                DELETE FROM notification_outbox
                WHERE delivered_at IS NOT NULL AND delivered_at < ?
                """,
                (notifications_before.isoformat(),),
            ).rowcount
            runs = connection.execute(
                """
                DELETE FROM check_runs
                WHERE checked_at < ?
                  AND id NOT IN (SELECT MAX(id) FROM check_runs GROUP BY job_id)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM transitions t
                      JOIN notification_outbox n ON n.transition_id = t.id
                      WHERE t.run_id = check_runs.id AND n.delivered_at IS NULL
                  )
                """,
                (observations_before.isoformat(),),
            ).rowcount
            connection.commit()
            return RetentionResult(runs, completed)
        except sqlite3.Error as error:
            connection.rollback()
            raise StorageError(f"could not prune database history: {error}") from error
        finally:
            connection.close()

    def status(self) -> StorageStatus:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT COUNT(*) AS check_runs, MIN(checked_at) AS oldest_observation,
                       (SELECT COUNT(*) FROM notification_outbox
                        WHERE delivered_at IS NULL) AS pending_notifications
                FROM check_runs
                """
            ).fetchone()
            return StorageStatus(
                database_bytes=self.path.stat().st_size,
                oldest_observation=datetime.fromisoformat(row["oldest_observation"])
                if row["oldest_observation"] is not None
                else None,
                check_runs=row["check_runs"],
                pending_notifications=row["pending_notifications"],
            )
        except (OSError, sqlite3.Error, ValueError) as error:
            raise StorageError(f"could not inspect database status: {error}") from error
        finally:
            connection.close()

    def list_pending_notifications(
        self, limit: int = 100
    ) -> tuple[PendingNotification, ...]:
        if limit <= 0:
            return ()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT id, channel, job_id, transition_type, opportunity_id,
                       opportunity_title, current_availability, booking_url,
                       checked_at, attempts
                FROM notification_outbox
                WHERE delivered_at IS NULL
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return tuple(
                PendingNotification(
                    id=row["id"],
                    channel=row["channel"],
                    job_id=row["job_id"],
                    transition_type=TransitionType(row["transition_type"]),
                    opportunity_id=row["opportunity_id"],
                    opportunity_title=row["opportunity_title"],
                    current_availability=Availability(row["current_availability"])
                    if row["current_availability"] is not None
                    else None,
                    booking_url=row["booking_url"],
                    checked_at=datetime.fromisoformat(row["checked_at"]),
                    attempts=row["attempts"],
                )
                for row in rows
            )
        except (sqlite3.Error, TypeError, ValueError) as error:
            raise StorageError(
                f"could not read pending notifications: {error}"
            ) from error
        finally:
            connection.close()

    def mark_notification_delivered(
        self, notification_id: int, delivered_at: datetime
    ) -> None:
        self._update_notification(
            notification_id,
            """
            UPDATE notification_outbox
            SET delivered_at = ?, last_attempt_at = ?, attempts = attempts + 1,
                last_error = NULL
            WHERE id = ? AND delivered_at IS NULL
            """,
            (delivered_at.isoformat(), delivered_at.isoformat(), notification_id),
        )

    def record_notification_failure(
        self, notification_id: int, attempted_at: datetime, error: str
    ) -> None:
        self._update_notification(
            notification_id,
            """
            UPDATE notification_outbox
            SET last_attempt_at = ?, attempts = attempts + 1, last_error = ?
            WHERE id = ? AND delivered_at IS NULL
            """,
            (attempted_at.isoformat(), error[:2000], notification_id),
        )

    def _update_notification(
        self, notification_id: int, statement: str, parameters: tuple
    ) -> None:
        connection = self._connect()
        try:
            cursor = connection.execute(statement, parameters)
            if cursor.rowcount != 1:
                raise StorageError(
                    f"pending notification {notification_id} was not found"
                )
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            raise StorageError(
                f"could not update notification {notification_id}: {error}"
            ) from error
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = None
        try:
            connection = self._connect()
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise StorageError(
                    f"database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}"
                )
            if version == 0:
                connection.executescript(
                    """
                    CREATE TABLE check_runs (
                        id INTEGER PRIMARY KEY,
                        job_id TEXT NOT NULL,
                        checked_at TEXT NOT NULL
                    );
                    CREATE INDEX check_runs_job_latest
                        ON check_runs (job_id, id DESC);

                    CREATE TABLE opportunity_observations (
                        run_id INTEGER NOT NULL REFERENCES check_runs(id)
                            ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        opportunity_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        availability TEXT NOT NULL,
                        starts_at TEXT,
                        booking_url TEXT,
                        attributes_json TEXT NOT NULL,
                        PRIMARY KEY (run_id, opportunity_id),
                        UNIQUE (run_id, position)
                    );

                    CREATE TABLE transitions (
                        id INTEGER PRIMARY KEY,
                        run_id INTEGER NOT NULL REFERENCES check_runs(id)
                            ON DELETE CASCADE,
                        opportunity_id TEXT NOT NULL,
                        transition_type TEXT NOT NULL,
                        previous_availability TEXT,
                        current_availability TEXT
                    );

                    CREATE TABLE notification_outbox (
                        id INTEGER PRIMARY KEY,
                        transition_id INTEGER NOT NULL REFERENCES transitions(id)
                            ON DELETE CASCADE,
                        channel TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        transition_type TEXT NOT NULL,
                        opportunity_id TEXT NOT NULL,
                        opportunity_title TEXT NOT NULL,
                        current_availability TEXT,
                        booking_url TEXT,
                        checked_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at TEXT,
                        last_error TEXT,
                        delivered_at TEXT,
                        UNIQUE (transition_id, channel)
                    );
                    CREATE INDEX notification_outbox_pending
                        ON notification_outbox (delivered_at, id);

                    PRAGMA user_version = 2;
                    """
                )
            elif version == 1:
                connection.executescript(
                    """
                    CREATE TABLE notification_outbox (
                        id INTEGER PRIMARY KEY,
                        transition_id INTEGER NOT NULL REFERENCES transitions(id)
                            ON DELETE CASCADE,
                        channel TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        transition_type TEXT NOT NULL,
                        opportunity_id TEXT NOT NULL,
                        opportunity_title TEXT NOT NULL,
                        current_availability TEXT,
                        booking_url TEXT,
                        checked_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at TEXT,
                        last_error TEXT,
                        delivered_at TEXT,
                        UNIQUE (transition_id, channel)
                    );
                    CREATE INDEX notification_outbox_pending
                        ON notification_outbox (delivered_at, id);
                    PRAGMA user_version = 2;
                    """
                )
        except StorageError:
            raise
        except sqlite3.Error as error:
            raise StorageError(
                f"could not initialize database {self.path}: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.path, timeout=10)
        except sqlite3.Error as error:
            raise StorageError(
                f"could not open database {self.path}: {error}"
            ) from error
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _get_latest(connection: sqlite3.Connection, job_id: str) -> CheckResult | None:
        run = connection.execute(
            """
            SELECT id, checked_at
            FROM check_runs
            WHERE job_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        if run is None:
            return None
        rows = connection.execute(
            """
            SELECT opportunity_id, title, availability, starts_at,
                   booking_url, attributes_json
            FROM opportunity_observations
            WHERE run_id = ?
            ORDER BY position
            """,
            (run["id"],),
        ).fetchall()
        opportunities = tuple(
            Opportunity(
                id=row["opportunity_id"],
                title=row["title"],
                availability=Availability(row["availability"]),
                starts_at=datetime.fromisoformat(row["starts_at"])
                if row["starts_at"] is not None
                else None,
                booking_url=row["booking_url"],
                attributes=json.loads(row["attributes_json"]),
            )
            for row in rows
        )
        return CheckResult(
            checked_at=datetime.fromisoformat(run["checked_at"]),
            opportunities=opportunities,
        )
