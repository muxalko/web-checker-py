"""Durable outbox dispatcher with per-channel delivery isolation."""

from datetime import UTC, datetime
from typing import Protocol

from web_checker.notifications.models import DeliverySummary, PendingNotification
from web_checker.notifications.registry import NotifierRegistry


class NotificationOutbox(Protocol):
    def list_pending_notifications(
        self, limit: int = 100
    ) -> tuple[PendingNotification, ...]: ...

    def mark_notification_delivered(
        self, notification_id: int, delivered_at: datetime
    ) -> None: ...

    def record_notification_failure(
        self, notification_id: int, attempted_at: datetime, error: str
    ) -> None: ...


class NotificationService:
    """Attempts pending deliveries while leaving failures available for retry."""

    def __init__(
        self,
        outbox: NotificationOutbox,
        registry: NotifierRegistry,
    ) -> None:
        self._outbox = outbox
        self._registry = registry

    async def dispatch_pending(self, limit: int = 100) -> DeliverySummary:
        delivered = 0
        failed = 0
        for notification in self._outbox.list_pending_notifications(limit):
            attempted_at = datetime.now(UTC)
            try:
                notifier = self._registry.get(notification.channel)
                await notifier.send(notification)
            except Exception as error:
                self._outbox.record_notification_failure(
                    notification.id,
                    attempted_at,
                    str(error),
                )
                failed += 1
                continue
            self._outbox.mark_notification_delivered(
                notification.id,
                attempted_at,
            )
            delivered += 1
        return DeliverySummary(delivered=delivered, failed=failed)
