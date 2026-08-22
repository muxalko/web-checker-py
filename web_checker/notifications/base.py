"""Contract implemented by notification delivery adapters."""

from typing import Protocol, runtime_checkable

from web_checker.notifications.models import OperationalAlert, PendingNotification

Notification = PendingNotification | OperationalAlert


@runtime_checkable
class Notifier(Protocol):
    """Delivers one durable notification through a named channel."""

    @property
    def name(self) -> str: ...

    async def send(self, notification: Notification) -> None: ...
