import asyncio
import os
import time
from datetime import UTC, datetime

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
from web_checker.notifications.email import EmailNotifier, SMTPSettings
from web_checker.notifications.models import NotificationPlan
from web_checker.notifications.registry import NotifierRegistry
from web_checker.notifications.service import NotificationService
from web_checker.storage import SQLiteObservationStore

SMTP_HOST = os.getenv("WEB_CHECKER_TEST_SMTP_HOST")
MAILPIT_API = os.getenv("WEB_CHECKER_TEST_MAILPIT_API")

pytestmark = pytest.mark.skipif(
    not SMTP_HOST or not MAILPIT_API,
    reason="local Mailpit integration is opt-in",
)


def test_mock_draw_is_captured_as_one_generic_email(tmp_path):
    assert SMTP_HOST is not None
    assert MAILPIT_API is not None
    with httpx.Client(base_url=MAILPIT_API, timeout=5) as mailpit:
        starting_total = (
            mailpit.get("/api/v1/messages").raise_for_status().json()["total"]
        )

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

    driver = WelcomeBCHighImpactDriver(
        transport=httpx.MockTransport(handler),
        clock=lambda: datetime(2026, 8, 13, 20, 0, tzinfo=UTC),
    )
    store = SQLiteObservationStore(tmp_path / "observations.db")
    notifier = EmailNotifier(
        SMTPSettings(
            host=SMTP_HOST,
            port=1025,
            security="none",
            sender="alerts@example.test",
            recipients=("operator@example.test",),
        )
    )
    service = CheckService(
        DriverRegistry([driver]),
        store,
        NotificationService(store, NotifierRegistry([notifier])),
    )
    job = JobDefinition(
        id="welcomebc",
        driver=driver.name,
        config={"url": "http://mock-site/welcomebc/invitations-to-apply"},
        notifications=NotificationPlan(
            channels=("email",),
            on=frozenset({TransitionType.APPEARED}),
        ),
    )

    baseline = asyncio.run(service.check(job))
    assert baseline.baseline_created is True
    assert baseline.delivery.delivered == 0
    client.post("/__control/welcomebc/publish")

    published = asyncio.run(service.check(job))

    assert published.delivery.delivered == 1
    message = _wait_for_latest_message(MAILPIT_API)
    assert message["Subject"] == (
        "[Web Checker] BC PNP High Economic Impact draw on August 13, 2026 "
        "(450 invitations)"
    )
    assert message["Text"] == (
        "BC PNP High Economic Impact draw on August 13, 2026 (450 invitations)\r\n"
        "\r\n"
        "http://mock-site/welcomebc/invitations-to-apply\r\n"
    )
    assert [recipient["Address"] for recipient in message["To"]] == [
        "operator@example.test"
    ]

    unchanged = asyncio.run(service.check(job))
    assert unchanged.delivery.delivered == 0
    with httpx.Client(base_url=MAILPIT_API, timeout=5) as mailpit:
        messages = mailpit.get("/api/v1/messages").raise_for_status().json()
    assert messages["total"] == starting_total + 1


def _wait_for_latest_message(api_url):
    deadline = time.monotonic() + 5
    with httpx.Client(base_url=api_url, timeout=5) as mailpit:
        while time.monotonic() < deadline:
            response = mailpit.get("/api/v1/message/latest")
            if response.status_code == 200:
                return response.json()
            if response.status_code != 404:
                response.raise_for_status()
            time.sleep(0.05)
    raise AssertionError("Mailpit did not capture an email within 5 seconds")
