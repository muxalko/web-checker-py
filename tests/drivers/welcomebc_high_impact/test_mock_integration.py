import asyncio

import httpx
import pytest

from mock_site.app import create_app
from web_checker.config import JobDefinition
from web_checker.core.service import CheckService
from web_checker.core.transitions import TransitionType
from web_checker.drivers.registry import DriverRegistry
from web_checker.drivers.welcomebc_high_impact.driver import (
    WelcomeBCHighImpactDriver,
)
from web_checker.drivers.welcomebc_high_impact.errors import WelcomeBCParseError
from web_checker.notifications.fake import FakeNotifier
from web_checker.notifications.models import NotificationPlan
from web_checker.notifications.registry import NotifierRegistry
from web_checker.notifications.service import NotificationService
from web_checker.storage import SQLiteObservationStore

MOCK_URL = "http://mock-site:8080/welcomebc/invitations-to-apply"


def create_service(tmp_path):
    app = create_app({"TESTING": True, "MOCK_SITE_CONTROLS_ENABLED": True})
    client = app.test_client()

    def handler(request):
        response = client.open(
            request.url.path,
            method=request.method,
            query_string=request.url.query.decode(),
        )
        return httpx.Response(
            response.status_code,
            content=response.data,
            headers=dict(response.headers),
            request=request,
        )

    store = SQLiteObservationStore(tmp_path / "welcomebc.db")
    notifier = FakeNotifier()
    driver = WelcomeBCHighImpactDriver(transport=httpx.MockTransport(handler))
    service = CheckService(
        DriverRegistry([driver]),
        store,
        NotificationService(store, NotifierRegistry([notifier])),
    )
    job = JobDefinition(
        id="welcomebc-high-impact",
        driver=driver.name,
        config={"url": MOCK_URL},
        notifications=NotificationPlan(
            channels=("fake",),
            on=frozenset({TransitionType.APPEARED}),
        ),
    )
    return client, job, service, store, notifier


def test_published_draw_sends_exactly_one_appeared_notification(tmp_path):
    client, job, service, _, notifier = create_service(tmp_path)

    baseline = asyncio.run(service.check(job))
    client.post("/__control/welcomebc/publish")
    changed = asyncio.run(service.check(job))
    unchanged = asyncio.run(service.check(job))

    assert baseline.baseline_created is True
    assert baseline.transitions == ()
    assert [transition.type for transition in changed.transitions] == [
        TransitionType.APPEARED
    ]
    assert changed.transitions[0].opportunity_id == ("high-economic-impact-2026-08-13")
    assert changed.delivery.delivered == 1
    assert unchanged.transitions == ()
    assert unchanged.delivery.delivered == 0
    assert len(notifier.notifications) == 1
    assert notifier.notifications[0].transition_type is TransitionType.APPEARED
    assert notifier.notifications[0].opportunity_title == (
        "BC PNP High Economic Impact draw on August 13, 2026 (450 invitations)"
    )


def test_malformed_mock_response_does_not_replace_last_snapshot(tmp_path):
    client, job, service, store, _ = create_service(tmp_path)
    asyncio.run(service.check(job))
    previous = store.get_latest(job.id)
    client.patch("/__control/welcomebc/behavior", json={"malformed": True})

    with pytest.raises(WelcomeBCParseError, match="Skills Immigration"):
        asyncio.run(service.check(job))

    assert store.get_latest(job.id) == previous
