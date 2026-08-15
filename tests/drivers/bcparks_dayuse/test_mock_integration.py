import asyncio

import httpx

from mock_site.app import create_app
from web_checker.core.models import Availability
from web_checker.drivers.bcparks_dayuse.driver import BCParksDayUseDriver

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
