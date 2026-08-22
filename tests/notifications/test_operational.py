import asyncio

from web_checker.notifications.operational import OperationalAlertService
from web_checker.notifications.registry import NotifierRegistry
from web_checker.storage import SQLiteObservationStore


class FailingNotifier:
    name = "email"

    async def send(self, notification):
        raise RuntimeError("smtp unavailable")


def test_smtp_alert_failure_remains_pending_and_is_logged(tmp_path):
    store = SQLiteObservationStore(tmp_path / "state.db")
    events = []
    service = OperationalAlertService(
        store,
        NotifierRegistry([FailingNotifier()]),
        event_sink=events.append,
    )

    asyncio.run(
        service.check_failed(
            "job",
            "provider unavailable",
            alert_after=1,
            channels=("email",),
        )
    )

    pending = store.list_pending_operational_alerts()
    assert len(pending) == 1
    assert pending[0].attempts == 1
    assert any("event=operational_alert_raised" in event for event in events)
    assert any(
        "event=operational_alert_delivery_failed" in event
        and "smtp unavailable" in event
        for event in events
    )
