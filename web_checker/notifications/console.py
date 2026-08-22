"""Human-readable console notification adapter."""

import sys
from typing import TextIO

from web_checker.notifications.models import OperationalAlert, PendingNotification


class ConsoleNotifier:
    """Writes notifications to a text stream."""

    name = "console"

    def __init__(self, output: TextIO | None = None) -> None:
        self._output = output or sys.stdout

    async def send(self, notification: PendingNotification | OperationalAlert) -> None:
        if isinstance(notification, OperationalAlert):
            print(
                f"Operational alert: {notification.title} — {notification.detail}",
                file=self._output,
            )
            return
        booking = f" {notification.booking_url}" if notification.booking_url else ""
        print(
            f"Notification: {notification.job_id} "
            f"{notification.transition_type.value} — "
            f"{notification.opportunity_title}{booking}",
            file=self._output,
        )
