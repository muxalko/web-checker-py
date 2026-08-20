import asyncio

import httpx
import pytest

from mock_site.app import create_app
from web_checker.config import JobDefinition
from web_checker.core.models import Availability
from web_checker.core.service import CheckService
from web_checker.core.transitions import TransitionType
from web_checker.drivers.bcparks_dayuse.driver import BCParksDayUseDriver
from web_checker.drivers.bcparks_dayuse.errors import BCParksParseError
from web_checker.drivers.registry import DriverRegistry
from web_checker.notifications.fake import FakeNotifier
from web_checker.notifications.models import NotificationPlan
from web_checker.notifications.registry import NotifierRegistry
from web_checker.notifications.service import NotificationService
from web_checker.storage import SQLiteObservationStore

FACILITY = "Alouette Lake South Beach Day-Use Parking Lot"


class FlaskAsyncTransport(httpx.AsyncBaseTransport):
    def __init__(self, client):
        self.client = client

    async def handle_async_request(self, request):
        response = self.client.open(
            path=request.url.raw_path.decode(),
            method=request.method,
            headers=dict(request.headers),
            data=await request.aread(),
        )
        return httpx.Response(
            response.status_code,
            content=response.get_data(),
            headers=dict(response.headers),
            request=request,
        )


def run_check(driver):
    return asyncio.run(
        driver.check(
            {
                "base_url": "http://mock-site/bcparks",
                "park_id": "0008",
                "facility": FACILITY,
                "slot": "AM",
            }
        )
    )


def create_service(tmp_path):
    app = create_app({"TESTING": True, "MOCK_SITE_CONTROLS_ENABLED": True})
    client = app.test_client()
    store = SQLiteObservationStore(tmp_path / "bcparks.db")
    notifier = FakeNotifier()
    driver = BCParksDayUseDriver(transport=FlaskAsyncTransport(client))
    service = CheckService(
        DriverRegistry([driver]),
        store,
        NotificationService(store, NotifierRegistry([notifier])),
    )
    job = JobDefinition(
        id="golden-ears-south-beach-am",
        driver=driver.name,
        config={
            "base_url": "http://mock-site/bcparks",
            "park_id": "0008",
            "facility": FACILITY,
            "slot": "AM",
        },
        notifications=NotificationPlan(
            channels=("fake",),
            on=frozenset({TransitionType.BECAME_AVAILABLE}),
        ),
    )
    return client, job, service, store, notifier


def test_driver_uses_production_shaped_mock_for_transition_scenario():
    app = create_app({"TESTING": True, "MOCK_SITE_CONTROLS_ENABLED": True})
    client = app.test_client()
    driver = BCParksDayUseDriver(transport=FlaskAsyncTransport(client))

    baseline = run_check(driver)
    assert baseline.opportunities[1].availability is Availability.UNAVAILABLE

    update = {
        "park_id": "0008",
        "facility": FACILITY,
        "date": "2026-08-16",
        "slot": "AM",
        "capacity": "Low",
        "max": 1,
    }
    response = client.patch("/__control/bcparks/reservations", json=update)
    assert response.status_code == 200

    changed = run_check(driver)
    assert changed.opportunities[1].availability is Availability.AVAILABLE


def test_mock_transition_sends_one_notification_without_duplicates(tmp_path):
    client, job, service, _, notifier = create_service(tmp_path)

    baseline = asyncio.run(service.check(job))
    client.patch(
        "/__control/bcparks/reservations",
        json={
            "park_id": "0008",
            "facility": FACILITY,
            "date": "2026-08-16",
            "slot": "AM",
            "capacity": "Low",
            "max": 1,
        },
    )
    changed = asyncio.run(service.check(job))
    unchanged = asyncio.run(service.check(job))

    assert baseline.baseline_created is True
    assert baseline.transitions == ()
    assert [transition.type for transition in changed.transitions] == [
        TransitionType.BECAME_AVAILABLE
    ]
    assert changed.delivery.delivered == 1
    assert unchanged.transitions == ()
    assert unchanged.delivery.delivered == 0
    assert len(notifier.notifications) == 1
    assert notifier.notifications[0].opportunity_id.endswith("2026-08-16:AM")


def test_malformed_mock_response_preserves_last_snapshot(tmp_path):
    client, job, service, store, _ = create_service(tmp_path)
    asyncio.run(service.check(job))
    previous = store.get_latest(job.id)
    client.patch("/__control/bcparks/behavior", json={"malformed": True})

    with pytest.raises(BCParksParseError, match="Invalid JSON"):
        asyncio.run(service.check(job))

    assert store.get_latest(job.id) == previous
