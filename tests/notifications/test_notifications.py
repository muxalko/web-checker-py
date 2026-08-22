import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from web_checker.core.models import Availability
from web_checker.core.transitions import TransitionType
from web_checker.notifications.console import ConsoleNotifier
from web_checker.notifications.fake import FakeNotifier
from web_checker.notifications.models import (
    MAX_DIGEST_ITEMS,
    NotificationItem,
    PendingNotification,
)
from web_checker.notifications.registry import (
    NotifierRegistry,
    NotifierRegistryError,
    UnknownNotifierError,
)
from web_checker.notifications.service import NotificationService


def pending(channel="fake"):
    return PendingNotification(
        id=1,
        channel=channel,
        job_id="job",
        checked_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        items=(
            NotificationItem(
                transition_type=TransitionType.BECAME_AVAILABLE,
                opportunity_id="pass",
                opportunity_title="Morning Pass",
                current_availability=Availability.AVAILABLE,
                starts_at=datetime(2026, 8, 22, 9, 0, tzinfo=UTC),
                booking_url="https://example.test/book",
            ),
        ),
        part_number=1,
        part_count=1,
        attempts=0,
    )


class FakeOutbox:
    def __init__(self, notifications):
        self.notifications = tuple(notifications)
        self.delivered = []
        self.failures = []

    def list_pending_notifications(self, limit=100):
        return self.notifications[:limit]

    def mark_notification_delivered(self, notification_id, delivered_at):
        self.delivered.append(notification_id)

    def record_notification_failure(self, notification_id, attempted_at, error):
        self.failures.append((notification_id, error))


def test_registry_resolves_and_rejects_duplicates():
    notifier = FakeNotifier()
    registry = NotifierRegistry([notifier])

    assert registry.get("fake") is notifier
    with pytest.raises(NotifierRegistryError, match="already registered"):
        registry.register(FakeNotifier())
    with pytest.raises(UnknownNotifierError, match="registered channels: fake"):
        registry.get("missing")


def test_dispatch_marks_successful_delivery():
    notification = pending()
    outbox = FakeOutbox([notification])
    notifier = FakeNotifier()

    summary = asyncio.run(
        NotificationService(outbox, NotifierRegistry([notifier])).dispatch_pending()
    )

    assert notifier.notifications == [notification]
    assert outbox.delivered == [1]
    assert outbox.failures == []
    assert summary.delivered == 1
    assert summary.failed == 0


def test_dispatch_isolates_failures_and_leaves_them_pending():
    outbox = FakeOutbox([pending(), pending(channel="missing")])
    notifier = FakeNotifier(error=RuntimeError("delivery unavailable"))

    summary = asyncio.run(
        NotificationService(outbox, NotifierRegistry([notifier])).dispatch_pending()
    )

    assert outbox.delivered == []
    assert outbox.failures == [
        (1, "delivery unavailable"),
        (1, "Unknown notification channel 'missing'; registered channels: fake"),
    ]
    assert summary.failed == 2


def test_console_notifier_formats_message(capsys):
    asyncio.run(ConsoleNotifier().send(pending(channel="console")))

    assert capsys.readouterr().out == (
        "Notification: job — 1 change(s)\n"
        "- became_available: Morning Pass | availability=available | "
        "starts_at=2026-08-22T09:00:00+00:00 | "
        "link=https://example.test/book\n"
    )


def test_digest_rejects_empty_or_oversized_payloads():
    notification = pending()

    with pytest.raises(ValueError, match="at least one"):
        replace(notification, items=())
    with pytest.raises(ValueError, match=f"cannot exceed {MAX_DIGEST_ITEMS}"):
        replace(
            notification,
            items=notification.items * (MAX_DIGEST_ITEMS + 1),
        )
