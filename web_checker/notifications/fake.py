"""Capturing notification adapter for tests and local development."""

from web_checker.notifications.models import PendingNotification


class FakeNotifier:
    """Captures messages and can simulate a delivery failure."""

    name = "fake"

    def __init__(self, *, error: Exception | None = None) -> None:
        self.notifications: list[PendingNotification] = []
        self.error = error

    async def send(self, notification: PendingNotification) -> None:
        if self.error is not None:
            raise self.error
        self.notifications.append(notification)
