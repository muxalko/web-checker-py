"""Transactional SQLite persistence for successful check snapshots."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from web_checker.core.models import Availability, CheckResult, Opportunity
from web_checker.core.service import RecordOutcome
from web_checker.core.transitions import (
    TransitionType,
    detect_initial_transitions,
    detect_transitions,
)
from web_checker.notifications.models import (
    MAX_DIGEST_ITEMS,
    NotificationItem,
    NotificationPlan,
    OperationalAlert,
    PendingNotification,
)

SCHEMA_VERSION = 4


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


@dataclass(frozen=True)
class JobStatus:
    job_id: str
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    availability: dict[str, int]
    pending_deliveries: int
    failed_deliveries: int


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
            connection.execute(
                """
                INSERT INTO job_status (job_id, last_success_at, consecutive_failures)
                VALUES (?, ?, 0)
                ON CONFLICT(job_id) DO UPDATE SET
                    last_success_at = excluded.last_success_at,
                    consecutive_failures = 0,
                    last_error = NULL,
                    failure_alert_active = 0
                """,
                (job_id, result.checked_at.isoformat()),
            )
            connection.execute(
                "DELETE FROM operational_alert_outbox WHERE alert_key = ?",
                (f"check-failure:{job_id}",),
            )
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
            queued_items: list[tuple[int, NotificationItem]] = []
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
                    queued_items.append(
                        (
                            transition_cursor.lastrowid,
                            NotificationItem(
                                transition_type=transition.type,
                                opportunity_id=transition.opportunity_id,
                                opportunity_title=opportunity.title,
                                current_availability=(
                                    transition.current.availability
                                    if transition.current is not None
                                    else None
                                ),
                                starts_at=opportunity.starts_at,
                                booking_url=opportunity.booking_url,
                            ),
                        )
                    )
            self._enqueue_notification_digests(
                connection,
                run_id=run_id,
                job_id=job_id,
                checked_at=result.checked_at,
                channels=plan.channels,
                items=queued_items,
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
                      FROM notification_outbox n
                      WHERE n.run_id = check_runs.id AND n.delivered_at IS NULL
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

    def record_check_failure(
        self,
        job_id: str,
        error: str,
        *,
        failed_at: datetime | None = None,
        alert_after: int = 3,
        channels: tuple[str, ...] = ("console",),
    ) -> bool:
        """Record terminal scheduled failure and enqueue one threshold alert."""
        occurred_at = failed_at or datetime.now(UTC)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO job_status (
                    job_id, last_failure_at, consecutive_failures, last_error
                ) VALUES (?, ?, 1, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    last_failure_at = excluded.last_failure_at,
                    consecutive_failures = consecutive_failures + 1,
                    last_error = excluded.last_error
                """,
                (job_id, occurred_at.isoformat(), error[:2000]),
            )
            row = connection.execute(
                """
                SELECT consecutive_failures, failure_alert_active
                FROM job_status WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            raised = (
                row["consecutive_failures"] >= alert_after
                and not row["failure_alert_active"]
            )
            if raised:
                self._enqueue_operational_alert(
                    connection,
                    key=f"check-failure:{job_id}",
                    job_id=job_id,
                    title=f"Repeated check failures: {job_id}",
                    detail=(
                        f"{row['consecutive_failures']} consecutive scheduled checks "
                        f"failed. Last error: {error[:1000]}"
                    ),
                    created_at=occurred_at,
                    channels=channels,
                )
                connection.execute(
                    "UPDATE job_status SET failure_alert_active = 1 WHERE job_id = ?",
                    (job_id,),
                )
            connection.commit()
            return raised
        except sqlite3.Error as database_error:
            connection.rollback()
            raise StorageError(
                f"could not record check failure for {job_id!r}: {database_error}"
            ) from database_error
        finally:
            connection.close()

    def list_job_statuses(self) -> tuple[JobStatus, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT s.job_id, s.last_success_at, s.last_failure_at,
                       s.consecutive_failures, s.last_error,
                       (SELECT COUNT(*) FROM notification_outbox n
                        WHERE n.job_id = s.job_id AND n.delivered_at IS NULL) pending,
                       (SELECT COUNT(*) FROM notification_outbox n
                        WHERE n.job_id = s.job_id AND n.delivered_at IS NULL
                          AND n.attempts > 0) failed
                FROM job_status s ORDER BY s.job_id
                """
            ).fetchall()
            statuses = []
            for row in rows:
                availability_rows = connection.execute(
                    """
                    SELECT o.availability, COUNT(*) count
                    FROM opportunity_observations o
                    WHERE o.run_id = (SELECT MAX(id) FROM check_runs WHERE job_id = ?)
                    GROUP BY o.availability
                    """,
                    (row["job_id"],),
                ).fetchall()
                statuses.append(
                    JobStatus(
                        job_id=row["job_id"],
                        last_success_at=datetime.fromisoformat(row["last_success_at"])
                        if row["last_success_at"]
                        else None,
                        last_failure_at=datetime.fromisoformat(row["last_failure_at"])
                        if row["last_failure_at"]
                        else None,
                        consecutive_failures=row["consecutive_failures"],
                        last_error=row["last_error"],
                        availability={
                            item["availability"]: item["count"]
                            for item in availability_rows
                        },
                        pending_deliveries=row["pending"],
                        failed_deliveries=row["failed"],
                    )
                )
            return tuple(statuses)
        except (sqlite3.Error, ValueError) as error:
            raise StorageError(f"could not read job status: {error}") from error
        finally:
            connection.close()

    def raise_delivery_backlog_alerts(
        self, *, alert_after: int, channels: tuple[str, ...]
    ) -> tuple[str, ...]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT s.job_id, s.delivery_alert_active, COUNT(n.id) count,
                       MAX(n.attempts) max_attempts
                FROM job_status s
                LEFT JOIN notification_outbox n
                  ON n.job_id = s.job_id AND n.delivered_at IS NULL
                GROUP BY s.job_id
                """
            ).fetchall()
            raised = []
            for row in rows:
                active = row["count"] > 0 and row["max_attempts"] >= alert_after
                if active and not row["delivery_alert_active"]:
                    self._enqueue_operational_alert(
                        connection,
                        key=f"delivery-backlog:{row['job_id']}",
                        job_id=row["job_id"],
                        title=f"Notification delivery backlog: {row['job_id']}",
                        detail=(
                            f"{row['count']} deliveries remain pending; the oldest "
                            f"has failed {row['max_attempts']} times. "
                            "Check worker logs."
                        ),
                        created_at=datetime.now(UTC),
                        channels=channels,
                    )
                    raised.append(row["job_id"])
                if not active and row["delivery_alert_active"]:
                    connection.execute(
                        "DELETE FROM operational_alert_outbox WHERE alert_key = ?",
                        (f"delivery-backlog:{row['job_id']}",),
                    )
                connection.execute(
                    "UPDATE job_status SET delivery_alert_active = ? WHERE job_id = ?",
                    (int(active), row["job_id"]),
                )
            connection.commit()
            return tuple(raised)
        except sqlite3.Error as error:
            connection.rollback()
            raise StorageError(
                f"could not evaluate delivery backlog: {error}"
            ) from error
        finally:
            connection.close()

    def list_pending_operational_alerts(
        self, limit: int = 100
    ) -> tuple[OperationalAlert, ...]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT id, channel, alert_key, job_id, title, detail,
                       created_at, attempts
                FROM operational_alert_outbox WHERE delivered_at IS NULL
                ORDER BY id LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return tuple(
                OperationalAlert(
                    id=row["id"],
                    channel=row["channel"],
                    key=row["alert_key"],
                    job_id=row["job_id"],
                    title=row["title"],
                    detail=row["detail"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    attempts=row["attempts"],
                )
                for row in rows
            )
        except (sqlite3.Error, ValueError) as error:
            raise StorageError(f"could not read operational alerts: {error}") from error
        finally:
            connection.close()

    def mark_operational_alert_delivered(
        self, alert_id: int, delivered_at: datetime
    ) -> None:
        self._update_operational_alert(alert_id, delivered_at, None)

    def record_operational_alert_failure(
        self, alert_id: int, attempted_at: datetime, error: str
    ) -> None:
        self._update_operational_alert(alert_id, attempted_at, error)

    def _update_operational_alert(
        self, alert_id: int, attempted_at: datetime, error: str | None
    ) -> None:
        connection = self._connect()
        try:
            if error is None:
                statement = """
                    UPDATE operational_alert_outbox
                    SET delivered_at=?, attempts=attempts+1, last_error=NULL
                    WHERE id=? AND delivered_at IS NULL
                """
                parameters = (attempted_at.isoformat(), alert_id)
            else:
                statement = """
                    UPDATE operational_alert_outbox
                    SET attempts=attempts+1, last_error=?
                    WHERE id=? AND delivered_at IS NULL
                """
                parameters = (error[:2000], alert_id)
            if connection.execute(statement, parameters).rowcount != 1:
                raise StorageError(
                    f"pending operational alert {alert_id} was not found"
                )
            connection.commit()
        except sqlite3.Error as database_error:
            raise StorageError(
                f"could not update operational alert: {database_error}"
            ) from database_error
        finally:
            connection.close()

    @staticmethod
    def _enqueue_operational_alert(
        connection, *, key, job_id, title, detail, created_at, channels
    ):
        for channel in channels:
            connection.execute(
                """INSERT INTO operational_alert_outbox
                (alert_key, channel, job_id, title, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (key, channel, job_id, title, detail, created_at.isoformat()),
            )

    @staticmethod
    def _enqueue_notification_digests(
        connection,
        *,
        run_id: int,
        job_id: str,
        checked_at: datetime,
        channels: tuple[str, ...],
        items: list[tuple[int, NotificationItem]],
    ) -> None:
        ordered = sorted(
            items,
            key=lambda queued: (
                queued[1].starts_at is None,
                queued[1].starts_at.isoformat() if queued[1].starts_at else "",
                queued[1].opportunity_title.casefold(),
                queued[1].opportunity_id,
                queued[1].transition_type.value,
            ),
        )
        chunks = tuple(
            ordered[position : position + MAX_DIGEST_ITEMS]
            for position in range(0, len(ordered), MAX_DIGEST_ITEMS)
        )
        for channel in channels:
            for part_index, chunk in enumerate(chunks):
                cursor = connection.execute(
                    """
                    INSERT INTO notification_outbox (
                        run_id, channel, job_id, checked_at, part_index, part_count
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        channel,
                        job_id,
                        checked_at.isoformat(),
                        part_index,
                        len(chunks),
                    ),
                )
                for position, (transition_id, item) in enumerate(chunk):
                    connection.execute(
                        """
                        INSERT INTO notification_outbox_items (
                            notification_id, position, transition_id, channel,
                            transition_type, opportunity_id, opportunity_title,
                            current_availability, starts_at, booking_url
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            cursor.lastrowid,
                            position,
                            transition_id,
                            channel,
                            item.transition_type.value,
                            item.opportunity_id,
                            item.opportunity_title,
                            item.current_availability.value
                            if item.current_availability is not None
                            else None,
                            item.starts_at.isoformat()
                            if item.starts_at is not None
                            else None,
                            item.booking_url,
                        ),
                    )

    def list_pending_notifications(
        self, limit: int = 100
    ) -> tuple[PendingNotification, ...]:
        if limit <= 0:
            return ()
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT id, channel, job_id, checked_at, part_index, part_count,
                       attempts
                FROM notification_outbox
                WHERE delivered_at IS NULL
                ORDER BY id
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            notifications = []
            for row in rows:
                item_rows = connection.execute(
                    """
                    SELECT transition_type, opportunity_id, opportunity_title,
                           current_availability, starts_at, booking_url
                    FROM notification_outbox_items
                    WHERE notification_id = ? ORDER BY position
                    """,
                    (row["id"],),
                ).fetchall()
                notifications.append(
                    PendingNotification(
                        id=row["id"],
                        channel=row["channel"],
                        job_id=row["job_id"],
                        checked_at=datetime.fromisoformat(row["checked_at"]),
                        items=tuple(
                            NotificationItem(
                                transition_type=TransitionType(item["transition_type"]),
                                opportunity_id=item["opportunity_id"],
                                opportunity_title=item["opportunity_title"],
                                current_availability=Availability(
                                    item["current_availability"]
                                )
                                if item["current_availability"] is not None
                                else None,
                                starts_at=datetime.fromisoformat(item["starts_at"])
                                if item["starts_at"] is not None
                                else None,
                                booking_url=item["booking_url"],
                            )
                            for item in item_rows
                        ),
                        part_number=row["part_index"] + 1,
                        part_count=row["part_count"],
                        attempts=row["attempts"],
                    )
                )
            return tuple(notifications)
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
                        run_id INTEGER NOT NULL REFERENCES check_runs(id)
                            ON DELETE CASCADE,
                        channel TEXT NOT NULL,
                        job_id TEXT NOT NULL,
                        checked_at TEXT NOT NULL,
                        part_index INTEGER NOT NULL,
                        part_count INTEGER NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_attempt_at TEXT,
                        last_error TEXT,
                        delivered_at TEXT,
                        UNIQUE (run_id, channel, part_index)
                    );
                    CREATE INDEX notification_outbox_pending
                        ON notification_outbox (delivered_at, id);

                    CREATE TABLE notification_outbox_items (
                        notification_id INTEGER NOT NULL
                            REFERENCES notification_outbox(id) ON DELETE CASCADE,
                        position INTEGER NOT NULL,
                        transition_id INTEGER NOT NULL REFERENCES transitions(id)
                            ON DELETE CASCADE,
                        channel TEXT NOT NULL,
                        transition_type TEXT NOT NULL,
                        opportunity_id TEXT NOT NULL,
                        opportunity_title TEXT NOT NULL,
                        current_availability TEXT,
                        starts_at TEXT,
                        booking_url TEXT,
                        PRIMARY KEY (notification_id, position),
                        UNIQUE (transition_id, channel)
                    );

                    CREATE TABLE job_status (
                        job_id TEXT PRIMARY KEY,
                        last_success_at TEXT,
                        last_failure_at TEXT,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        failure_alert_active INTEGER NOT NULL DEFAULT 0,
                        delivery_alert_active INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE operational_alert_outbox (
                        id INTEGER PRIMARY KEY,
                        alert_key TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        job_id TEXT,
                        title TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        delivered_at TEXT,
                        UNIQUE (alert_key, channel)
                    );
                    CREATE INDEX operational_alert_pending
                        ON operational_alert_outbox (delivered_at, id);

                    PRAGMA user_version = 4;
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
                version = 2
            if version == 2:
                connection.executescript(
                    """
                    CREATE TABLE job_status (
                        job_id TEXT PRIMARY KEY,
                        last_success_at TEXT,
                        last_failure_at TEXT,
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        failure_alert_active INTEGER NOT NULL DEFAULT 0,
                        delivery_alert_active INTEGER NOT NULL DEFAULT 0
                    );
                    CREATE TABLE operational_alert_outbox (
                        id INTEGER PRIMARY KEY,
                        alert_key TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        job_id TEXT,
                        title TEXT NOT NULL,
                        detail TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT,
                        delivered_at TEXT,
                        UNIQUE (alert_key, channel)
                    );
                    CREATE INDEX operational_alert_pending
                        ON operational_alert_outbox (delivered_at, id);
                    PRAGMA user_version = 3;
                    """
                )
                has_check_runs = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='check_runs'
                    """
                ).fetchone()
                if has_check_runs:
                    connection.execute(
                        """
                        INSERT INTO job_status (job_id, last_success_at)
                        SELECT job_id, MAX(checked_at)
                        FROM check_runs GROUP BY job_id
                        """
                    )
                    connection.commit()
                version = 3
            if version == 3:
                self._migrate_notification_digests(connection)
        except StorageError:
            raise
        except sqlite3.Error as error:
            raise StorageError(
                f"could not initialize database {self.path}: {error}"
            ) from error
        finally:
            if connection is not None:
                connection.close()

    @staticmethod
    def _migrate_notification_digests(connection: sqlite3.Connection) -> None:
        """Preserve v3 delivery state while adopting durable digest envelopes."""
        connection.executescript(
            """
            DROP INDEX IF EXISTS notification_outbox_pending;
            ALTER TABLE notification_outbox RENAME TO notification_outbox_v3;

            CREATE TABLE notification_outbox (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES check_runs(id) ON DELETE CASCADE,
                channel TEXT NOT NULL,
                job_id TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                part_count INTEGER NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                delivered_at TEXT,
                UNIQUE (run_id, channel, part_index)
            );
            CREATE INDEX notification_outbox_pending
                ON notification_outbox (delivered_at, id);

            CREATE TABLE notification_outbox_items (
                notification_id INTEGER NOT NULL
                    REFERENCES notification_outbox(id) ON DELETE CASCADE,
                position INTEGER NOT NULL,
                transition_id INTEGER NOT NULL REFERENCES transitions(id)
                    ON DELETE CASCADE,
                channel TEXT NOT NULL,
                transition_type TEXT NOT NULL,
                opportunity_id TEXT NOT NULL,
                opportunity_title TEXT NOT NULL,
                current_availability TEXT,
                starts_at TEXT,
                booking_url TEXT,
                PRIMARY KEY (notification_id, position),
                UNIQUE (transition_id, channel)
            );
            """
        )
        legacy_count = connection.execute(
            "SELECT COUNT(*) FROM notification_outbox_v3"
        ).fetchone()[0]
        if legacy_count:
            rows = connection.execute(
                """
                SELECT n.*, t.run_id
                FROM notification_outbox_v3 n
                JOIN transitions t ON t.id = n.transition_id
                ORDER BY t.run_id, n.channel, n.id
                """
            ).fetchall()
            group_counts: dict[tuple[int, str], int] = {}
            for row in rows:
                key = (row["run_id"], row["channel"])
                group_counts[key] = group_counts.get(key, 0) + 1
            group_positions: dict[tuple[int, str], int] = {}
            for row in rows:
                key = (row["run_id"], row["channel"])
                part_index = group_positions.get(key, 0)
                group_positions[key] = part_index + 1
                cursor = connection.execute(
                    """
                    INSERT INTO notification_outbox (
                        run_id, channel, job_id, checked_at, part_index, part_count,
                        attempts, last_attempt_at, last_error, delivered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["run_id"],
                        row["channel"],
                        row["job_id"],
                        row["checked_at"],
                        part_index,
                        group_counts[key],
                        row["attempts"],
                        row["last_attempt_at"],
                        row["last_error"],
                        row["delivered_at"],
                    ),
                )
                starts_at_row = connection.execute(
                    """
                    SELECT starts_at FROM opportunity_observations
                    WHERE run_id = ? AND opportunity_id = ?
                    """,
                    (row["run_id"], row["opportunity_id"]),
                ).fetchone()
                if starts_at_row is None:
                    starts_at_row = connection.execute(
                        """
                        SELECT o.starts_at
                        FROM check_runs r
                        JOIN opportunity_observations o ON o.run_id = r.id
                        WHERE r.job_id = ? AND r.id < ? AND o.opportunity_id = ?
                        ORDER BY r.id DESC LIMIT 1
                        """,
                        (row["job_id"], row["run_id"], row["opportunity_id"]),
                    ).fetchone()
                connection.execute(
                    """
                    INSERT INTO notification_outbox_items (
                        notification_id, position, transition_id, channel,
                        transition_type, opportunity_id, opportunity_title,
                        current_availability, starts_at, booking_url
                    ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cursor.lastrowid,
                        row["transition_id"],
                        row["channel"],
                        row["transition_type"],
                        row["opportunity_id"],
                        row["opportunity_title"],
                        row["current_availability"],
                        starts_at_row["starts_at"] if starts_at_row else None,
                        row["booking_url"],
                    ),
                )
        connection.executescript(
            """
            DROP TABLE notification_outbox_v3;
            PRAGMA user_version = 4;
            """
        )
        connection.commit()

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
