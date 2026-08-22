import sqlite3
from datetime import UTC, datetime

import pytest

from web_checker.core.models import Availability, CheckResult, Opportunity
from web_checker.core.transitions import TransitionType
from web_checker.notifications.models import MAX_DIGEST_ITEMS, NotificationPlan
from web_checker.storage import SQLiteObservationStore, StorageError


def opportunity(identifier, availability, **kwargs):
    return Opportunity(
        id=identifier,
        title=kwargs.pop("title", identifier),
        availability=availability,
        **kwargs,
    )


def result(*opportunities, minute=0):
    return CheckResult(
        checked_at=datetime(2026, 8, 10, 12, minute, tzinfo=UTC),
        opportunities=opportunities,
    )


def test_first_success_creates_baseline_without_transitions(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    snapshot = result(opportunity("pass", Availability.UNAVAILABLE))

    outcome = store.record_success("job", snapshot)

    assert outcome.baseline_created is True
    assert outcome.transitions == ()
    assert store.get_latest("job") == snapshot


def test_first_available_success_can_create_deduplicated_notifications(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteObservationStore(path)
    plan = NotificationPlan(
        channels=("email",),
        on=frozenset({TransitionType.INITIALLY_AVAILABLE}),
    )
    snapshot = result(opportunity("pass", Availability.AVAILABLE))

    outcome = store.record_success("job", snapshot, plan)
    store.record_success("job", result(*snapshot.opportunities, minute=1), plan)
    reopened = SQLiteObservationStore(path)

    assert outcome.baseline_created is True
    assert [item.type for item in outcome.transitions] == [
        TransitionType.INITIALLY_AVAILABLE
    ]
    assert [
        item.transition_type
        for notification in reopened.list_pending_notifications()
        for item in notification.items
    ] == [TransitionType.INITIALLY_AVAILABLE]


def test_initial_available_transition_is_quiet_when_not_selected(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    plan = NotificationPlan(
        channels=("email",),
        on=frozenset({TransitionType.BECAME_AVAILABLE}),
    )

    outcome = store.record_success(
        "job", result(opportunity("pass", Availability.AVAILABLE)), plan
    )

    assert [item.type for item in outcome.transitions] == [
        TransitionType.INITIALLY_AVAILABLE
    ]
    assert store.list_pending_notifications() == ()


def test_later_success_is_compared_and_survives_reopen(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteObservationStore(path)
    store.record_success("job", result(opportunity("pass", Availability.UNAVAILABLE)))
    changed = result(
        opportunity(
            "pass",
            Availability.AVAILABLE,
            starts_at=datetime(2026, 8, 22, 9, 0),
            booking_url="https://example.test/book",
            attributes={"capacity": "1"},
        ),
        minute=1,
    )

    outcome = store.record_success("job", changed)
    reopened = SQLiteObservationStore(path)

    assert outcome.baseline_created is False
    assert [item.type for item in outcome.transitions] == [
        TransitionType.BECAME_AVAILABLE
    ]
    assert reopened.get_latest("job") == changed


def test_unchanged_success_creates_no_transition(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    first = result(opportunity("pass", Availability.AVAILABLE))
    second = result(opportunity("pass", Availability.AVAILABLE), minute=1)
    store.record_success("job", first)

    assert store.record_success("job", second).transitions == ()


def test_jobs_have_independent_baselines(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    snapshot = result(opportunity("pass", Availability.AVAILABLE))

    assert store.record_success("first", snapshot).baseline_created is True
    assert store.record_success("second", snapshot).baseline_created is True


def test_serialization_failure_rolls_back_entire_check(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    invalid = result(
        opportunity("pass", Availability.AVAILABLE, attributes={"bad": object()})
    )

    with pytest.raises(StorageError, match="could not record"):
        store.record_success("job", invalid)

    assert store.get_latest("job") is None


def test_newer_schema_is_rejected(tmp_path):
    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()

    with pytest.raises(StorageError, match="newer than supported"):
        SQLiteObservationStore(path)


def test_matching_transitions_create_deduplicated_outbox_items(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    plan = NotificationPlan(
        channels=("console", "fake"),
        on=frozenset({TransitionType.BECAME_AVAILABLE}),
    )
    store.record_success(
        "job",
        result(opportunity("pass", Availability.UNAVAILABLE)),
        plan,
    )

    outcome = store.record_success(
        "job",
        result(opportunity("pass", Availability.AVAILABLE), minute=1),
        plan,
    )
    store.record_success(
        "job",
        result(opportunity("pass", Availability.AVAILABLE), minute=2),
        plan,
    )
    pending = store.list_pending_notifications()

    assert [item.type for item in outcome.transitions] == [
        TransitionType.BECAME_AVAILABLE
    ]
    assert [(item.channel, item.items[0].transition_type) for item in pending] == [
        ("console", TransitionType.BECAME_AVAILABLE),
        ("fake", TransitionType.BECAME_AVAILABLE),
    ]


def test_one_check_creates_ordered_provider_independent_digest_per_channel(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    plan = NotificationPlan(
        channels=("console",),
        on=frozenset({TransitionType.BECAME_AVAILABLE}),
    )
    store.record_success(
        "job",
        result(
            opportunity("later", Availability.UNAVAILABLE),
            opportunity("earlier", Availability.UNAVAILABLE),
        ),
        plan,
    )

    store.record_success(
        "job",
        result(
            opportunity(
                "later",
                Availability.AVAILABLE,
                title="Later pass",
                starts_at=datetime(2026, 8, 23, 9, 0, tzinfo=UTC),
                booking_url="https://example.test/later",
            ),
            opportunity(
                "earlier",
                Availability.AVAILABLE,
                title="Earlier pass",
                starts_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
                booking_url="https://example.test/earlier",
            ),
            minute=1,
        ),
        plan,
    )

    pending = store.list_pending_notifications()

    assert len(pending) == 1
    assert [item.opportunity_id for item in pending[0].items] == ["earlier", "later"]
    assert pending[0].items[0].current_availability is Availability.AVAILABLE
    assert pending[0].items[0].starts_at == datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
    assert pending[0].items[0].booking_url == "https://example.test/earlier"
    status = store.list_job_statuses()[0]
    assert status.pending_deliveries == 1
    assert status.failed_deliveries == 0


def test_large_check_splits_into_deterministic_bounded_digest_parts(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    plan = NotificationPlan(
        channels=("console",),
        on=frozenset({TransitionType.BECAME_AVAILABLE}),
    )
    identifiers = [f"pass-{position:02d}" for position in range(MAX_DIGEST_ITEMS + 1)]
    store.record_success(
        "job",
        result(*(opportunity(item, Availability.UNAVAILABLE) for item in identifiers)),
        plan,
    )
    store.record_success(
        "job",
        result(
            *(
                opportunity(item, Availability.AVAILABLE)
                for item in reversed(identifiers)
            ),
            minute=1,
        ),
        plan,
    )

    pending = store.list_pending_notifications()

    assert [len(notification.items) for notification in pending] == [
        MAX_DIGEST_ITEMS,
        1,
    ]
    assert [(item.part_number, item.part_count) for item in pending] == [(1, 2), (2, 2)]
    assert [item.opportunity_id for item in pending[0].items[:2]] == [
        "pass-00",
        "pass-01",
    ]


def test_delivery_and_failure_updates_outbox_state(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    plan = NotificationPlan(
        channels=("console",),
        on=frozenset({TransitionType.BECAME_AVAILABLE}),
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.UNAVAILABLE)), plan
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.AVAILABLE), minute=1), plan
    )
    notification = store.list_pending_notifications()[0]
    attempted_at = datetime(2026, 8, 10, 12, 2, tzinfo=UTC)

    store.record_notification_failure(notification.id, attempted_at, "temporary")
    retried = store.list_pending_notifications()[0]
    assert retried.attempts == 1

    store.mark_notification_delivered(retried.id, attempted_at)
    assert store.list_pending_notifications() == ()


def test_version_one_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE transitions (id INTEGER PRIMARY KEY);
        PRAGMA user_version = 1;
        """
    )
    connection.close()

    store = SQLiteObservationStore(path)

    assert store.list_pending_notifications() == ()


def test_version_two_database_backfills_job_health(tmp_path):
    path = tmp_path / "version-two.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE check_runs (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, checked_at TEXT NOT NULL
        );
        CREATE TABLE opportunity_observations (
            run_id INTEGER NOT NULL, availability TEXT NOT NULL
        );
        CREATE TABLE notification_outbox (
            job_id TEXT NOT NULL, delivered_at TEXT, attempts INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO check_runs (job_id, checked_at)
        VALUES ('existing', '2026-08-10T12:00:00+00:00');
        PRAGMA user_version = 2;
        """
    )
    connection.close()

    store = SQLiteObservationStore(path)

    status = store.list_job_statuses()[0]
    assert status.job_id == "existing"
    assert status.last_success_at == datetime(2026, 8, 10, 12, tzinfo=UTC)


def test_version_three_delivery_state_migrates_without_replaying_success(tmp_path):
    path = tmp_path / "version-three.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE check_runs (
            id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, checked_at TEXT NOT NULL
        );
        CREATE TABLE opportunity_observations (
            run_id INTEGER NOT NULL, position INTEGER NOT NULL,
            opportunity_id TEXT NOT NULL, title TEXT NOT NULL,
            availability TEXT NOT NULL, starts_at TEXT, booking_url TEXT,
            attributes_json TEXT NOT NULL
        );
        CREATE TABLE transitions (
            id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL,
            opportunity_id TEXT NOT NULL, transition_type TEXT NOT NULL,
            previous_availability TEXT, current_availability TEXT
        );
        CREATE TABLE notification_outbox (
            id INTEGER PRIMARY KEY, transition_id INTEGER NOT NULL,
            channel TEXT NOT NULL, job_id TEXT NOT NULL,
            transition_type TEXT NOT NULL, opportunity_id TEXT NOT NULL,
            opportunity_title TEXT NOT NULL, current_availability TEXT,
            booking_url TEXT, checked_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_attempt_at TEXT,
            last_error TEXT, delivered_at TEXT,
            UNIQUE (transition_id, channel)
        );
        CREATE INDEX notification_outbox_pending
            ON notification_outbox (delivered_at, id);
        CREATE TABLE job_status (
            job_id TEXT PRIMARY KEY, last_success_at TEXT, last_failure_at TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0, last_error TEXT,
            failure_alert_active INTEGER NOT NULL DEFAULT 0,
            delivery_alert_active INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE operational_alert_outbox (
            id INTEGER PRIMARY KEY, alert_key TEXT NOT NULL, channel TEXT NOT NULL,
            job_id TEXT, title TEXT NOT NULL, detail TEXT NOT NULL,
            created_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            last_error TEXT, delivered_at TEXT,
            UNIQUE (alert_key, channel)
        );
        INSERT INTO check_runs VALUES (1, 'job', '2026-08-10T12:00:00+00:00');
        INSERT INTO opportunity_observations VALUES
            (1, 0, 'first', 'First', 'available',
             '2026-08-22T09:00:00+00:00', 'https://example.test/first', '{}'),
            (1, 1, 'second', 'Second', 'available',
             '2026-08-23T09:00:00+00:00', 'https://example.test/second', '{}');
        INSERT INTO transitions VALUES
            (1, 1, 'first', 'became_available', 'unavailable', 'available'),
            (2, 1, 'second', 'became_available', 'unavailable', 'available');
        INSERT INTO notification_outbox VALUES
            (1, 1, 'email', 'job', 'became_available', 'first', 'First',
             'available', 'https://example.test/first',
             '2026-08-10T12:00:00+00:00', 1,
             '2026-08-10T12:01:00+00:00', NULL,
             '2026-08-10T12:01:00+00:00'),
            (2, 2, 'email', 'job', 'became_available', 'second', 'Second',
             'available', 'https://example.test/second',
             '2026-08-10T12:00:00+00:00', 2,
             '2026-08-10T12:02:00+00:00', 'smtp down', NULL);
        PRAGMA user_version = 3;
        """
    )
    connection.close()

    pending = SQLiteObservationStore(path).list_pending_notifications()

    assert len(pending) == 1
    assert pending[0].part_number == 2
    assert pending[0].part_count == 2
    assert pending[0].attempts == 2
    assert pending[0].items[0].opportunity_id == "second"
    assert pending[0].items[0].starts_at == datetime(2026, 8, 23, 9, 0, tzinfo=UTC)


def test_prune_preserves_latest_baseline_and_pending_delivery(tmp_path):
    path = tmp_path / "state.db"
    store = SQLiteObservationStore(path)
    plan = NotificationPlan(
        channels=("console",), on=frozenset({TransitionType.BECAME_AVAILABLE})
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.UNAVAILABLE)), plan
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.AVAILABLE), minute=1), plan
    )
    latest = result(opportunity("pass", Availability.AVAILABLE), minute=2)
    store.record_success("job", latest, plan)

    outcome = store.prune(
        observations_before=datetime(2027, 1, 1, tzinfo=UTC),
        notifications_before=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert outcome.check_runs_deleted == 1
    assert len(store.list_pending_notifications()) == 1
    assert store.get_latest("job") == latest
    assert store.status().check_runs == 2


def test_prune_removes_expired_completed_notifications(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    plan = NotificationPlan(
        channels=("console",), on=frozenset({TransitionType.BECAME_AVAILABLE})
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.UNAVAILABLE)), plan
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.AVAILABLE), minute=1), plan
    )
    pending = store.list_pending_notifications()[0]
    store.mark_notification_delivered(
        pending.id, datetime(2026, 8, 10, 12, 2, tzinfo=UTC)
    )

    outcome = store.prune(
        observations_before=datetime(2026, 1, 1, tzinfo=UTC),
        notifications_before=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert outcome.completed_notifications_deleted == 1
    assert store.status().pending_notifications == 0


def test_repeated_failures_are_recorded_and_alerted_once(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")

    assert store.record_check_failure("job", "first", alert_after=2) is False
    assert store.record_check_failure("job", "second", alert_after=2) is True
    assert store.record_check_failure("job", "third", alert_after=2) is False

    status = store.list_job_statuses()[0]
    assert status.consecutive_failures == 3
    assert status.last_error == "third"
    assert len(store.list_pending_operational_alerts()) == 1


def test_success_resets_failure_health_without_changing_snapshot_semantics(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    store.record_check_failure("job", "provider unavailable", alert_after=1)
    snapshot = result(opportunity("pass", Availability.AVAILABLE))

    store.record_success("job", snapshot)

    status = store.list_job_statuses()[0]
    assert status.consecutive_failures == 0
    assert status.last_error is None
    assert status.availability == {"available": 1}
    assert store.get_latest("job") == snapshot
    assert store.list_pending_operational_alerts() == ()


def test_delivery_backlog_alert_is_deduplicated(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    plan = NotificationPlan(
        channels=("console",), on=frozenset({TransitionType.BECAME_AVAILABLE})
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.UNAVAILABLE)), plan
    )
    store.record_success(
        "job", result(opportunity("pass", Availability.AVAILABLE), minute=1), plan
    )
    pending = store.list_pending_notifications()[0]
    attempted = datetime(2026, 8, 10, 12, 2, tzinfo=UTC)
    store.record_notification_failure(pending.id, attempted, "smtp down")

    assert store.raise_delivery_backlog_alerts(
        alert_after=1, channels=("console",)
    ) == ("job",)
    assert (
        store.raise_delivery_backlog_alerts(alert_after=1, channels=("console",)) == ()
    )
    status = store.list_job_statuses()[0]
    assert status.pending_deliveries == 1
    assert status.failed_deliveries == 1
    assert len(store.list_pending_operational_alerts()) == 1
