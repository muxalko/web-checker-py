"""Dispatch deduplicated operational alerts with an observable fallback."""

from collections.abc import Callable
from datetime import UTC, datetime

from web_checker.notifications.registry import NotifierRegistry


class OperationalAlertService:
    def __init__(
        self, store, registry: NotifierRegistry, *, event_sink: Callable[[str], None]
    ):
        self._store = store
        self._registry = registry
        self._event_sink = event_sink

    async def check_failed(
        self, job_id: str, error: str, *, alert_after: int, channels: tuple[str, ...]
    ) -> None:
        raised = self._store.record_check_failure(
            job_id, error, alert_after=alert_after, channels=channels
        )
        if raised:
            self._event_sink(
                f"job={job_id} event=operational_alert_raised kind=check_failure"
            )
        await self.dispatch_pending()

    async def dispatch_pending(self) -> None:
        for alert in self._store.list_pending_operational_alerts():
            attempted_at = datetime.now(UTC)
            try:
                await self._registry.get(alert.channel).send(alert)
            except Exception as error:
                self._store.record_operational_alert_failure(
                    alert.id, attempted_at, str(error)
                )
                self._event_sink(
                    f"job={alert.job_id or 'system'} "
                    "event=operational_alert_delivery_failed "
                    f"channel={alert.channel} error={type(error).__name__}: {error}"
                )
            else:
                self._store.mark_operational_alert_delivered(alert.id, attempted_at)
                self._event_sink(
                    f"job={alert.job_id or 'system'} "
                    f"event=operational_alert_delivered channel={alert.channel}"
                )

    async def delivery_backlog(
        self, *, alert_after: int, channels: tuple[str, ...]
    ) -> None:
        for job_id in self._store.raise_delivery_backlog_alerts(
            alert_after=alert_after, channels=channels
        ):
            self._event_sink(
                f"job={job_id} event=operational_alert_raised kind=delivery_backlog"
            )
        await self.dispatch_pending()
