import sqlite3
from datetime import UTC, datetime

import pytest

from web_checker.core.models import Availability, CheckResult, Opportunity
from web_checker.core.transitions import TransitionType
from web_checker.notifications.models import NotificationPlan
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
    assert [(item.channel, item.transition_type) for item in pending] == [
        ("console", TransitionType.BECAME_AVAILABLE),
        ("fake", TransitionType.BECAME_AVAILABLE),
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
