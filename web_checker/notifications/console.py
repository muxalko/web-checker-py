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
        part = (
            f" (part {notification.part_number}/{notification.part_count})"
            if notification.part_count > 1
            else ""
        )
        print(
            f"Notification: {notification.job_id} — "
            f"{len(notification.items)} change(s){part}",
            file=self._output,
        )
        for item in notification.items:
            availability = (
                item.current_availability.value
                if item.current_availability is not None
                else "not_present"
            )
            starts_at = item.starts_at.isoformat() if item.starts_at else "not provided"
            booking_url = item.booking_url or "not provided"
            print(
                f"- {item.transition_type.value}: {item.opportunity_title} | "
                f"availability={availability} | starts_at={starts_at} | "
                f"link={booking_url}",
                file=self._output,
            )
