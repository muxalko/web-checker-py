import asyncio
from copy import deepcopy
from datetime import UTC, datetime

import httpx
import pytest

from web_checker.core.models import Availability
from web_checker.drivers.bcparks_dayuse.driver import BCParksDayUseDriver
from web_checker.drivers.bcparks_dayuse.errors import (
    BCParksConfigurationError,
    BCParksParseError,
    BCParksProviderClosedError,
    BCParksResponseError,
    BCParksResponseTooLargeError,
    BCParksTimeoutError,
)

BASE_URL = "http://provider.test"
FACILITY = "Alouette Lake South Beach Day-Use Parking Lot"
CHECKED_AT = datetime(2026, 8, 15, 11, 5, tzinfo=UTC)


def make_config(**overrides):
    config = {
        "base_url": BASE_URL,
        "park_id": "0008",
        "facility": FACILITY,
        "slot": "AM",
    }
    config.update(overrides)
    return config


def run_check(driver, config=None):
    return asyncio.run(driver.check(make_config() if config is None else config))


def make_transport(fixture_json, *, facility_time=None, mutate=None):
    responses = {
        "/api/config": fixture_json("config.json"),
        "/api/park": fixture_json("parks.json"),
        "/api/facility": fixture_json("facilities-golden-ears.json"),
        "/api/reservation": fixture_json("reservations-south-beach.json"),
    }
    if facility_time is not None:
        for item in responses["/api/facility"]:
            item["currentTime"] = facility_time
    if mutate is not None:
        mutate(responses)

    def handler(request):
        value = responses[request.url.path]
        return httpx.Response(
            200,
            json=value,
            headers={"Content-Type": "application/json"},
        )

    return httpx.MockTransport(handler)


def test_observes_rolling_window_without_preopening_false_positive(fixture_json):
    driver = BCParksDayUseDriver(
        transport=make_transport(fixture_json), clock=lambda: CHECKED_AT
    )

    result = run_check(driver)

    assert result.checked_at == CHECKED_AT
    assert [item.availability for item in result.opportunities] == [
        Availability.UNAVAILABLE,
        Availability.UNAVAILABLE,
        Availability.UNKNOWN,
    ]
    assert result.opportunities[-1].attributes["booking_state"] == "pre_open"
    assert result.opportunities[-1].attributes["max_reservable"] == 1
    assert result.opportunities[0].booking_url == f"{BASE_URL}/dayuse/"


def test_open_inventory_becomes_available_at_opening_time(fixture_json):
    driver = BCParksDayUseDriver(
        transport=make_transport(fixture_json, facility_time="2026-08-15T14:00:00Z")
    )

    result = run_check(driver)

    assert result.opportunities[-1].availability is Availability.AVAILABLE
    assert result.opportunities[-1].starts_at.isoformat() == "2026-08-17T07:00:00-07:00"


def test_sends_only_anonymous_gets_and_optional_app_version(fixture_json):
    observed = []
    base = make_transport(fixture_json)

    async def handler(request):
        observed.append(request)
        return await base.handle_async_request(request)

    driver = BCParksDayUseDriver(transport=httpx.MockTransport(handler))

    run_check(driver, make_config(app_version="captured-version"))

    assert [request.method for request in observed] == ["GET"] * 4
    assert [request.url.path for request in observed] == [
        "/api/config",
        "/api/park",
        "/api/facility",
        "/api/reservation",
    ]
    assert all(
        request.headers["X-App-Version"] == "captured-version" for request in observed
    )
    assert all("authorization" not in request.headers for request in observed)
    assert observed[-1].url.params["park"] == "0008"
    assert observed[-1].url.params["facility"] == FACILITY


def test_closed_park_fails_without_requesting_facilities(fixture_json):
    def close_park(responses):
        responses["/api/park"][1]["status"] = "closed"

    driver = BCParksDayUseDriver(
        transport=make_transport(fixture_json, mutate=close_park)
    )

    with pytest.raises(BCParksProviderClosedError, match="not open"):
        run_check(driver)


def test_empty_reservation_response_fails(fixture_json):
    def empty_reservations(responses):
        responses["/api/reservation"] = {}

    driver = BCParksDayUseDriver(
        transport=make_transport(fixture_json, mutate=empty_reservations)
    )

    with pytest.raises(BCParksParseError, match="must not be empty"):
        run_check(driver)


def test_non_json_response_is_rejected():
    driver = BCParksDayUseDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="blocked")
        )
    )

    with pytest.raises(BCParksResponseError, match="non-JSON"):
        run_check(driver)


def test_response_size_is_bounded():
    driver = BCParksDayUseDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"{" + b"x" * 20,
                headers={"Content-Type": "application/json"},
            )
        )
    )

    with pytest.raises(BCParksResponseTooLargeError, match="exceeded 10 bytes"):
        run_check(driver, make_config(max_response_bytes=10))


def test_timeout_has_specific_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    driver = BCParksDayUseDriver(transport=httpx.MockTransport(handler))

    with pytest.raises(BCParksTimeoutError, match="2 seconds"):
        run_check(driver, make_config(timeout_seconds=2))


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"base_url": "ftp://provider.test"}, "HTTP or HTTPS"),
        ({"base_url": "https://user:secret@provider.test"}, "credentials"),
        ({"park_id": 8}, "park_id"),
        ({"facility": ""}, "facility"),
        ({"slot": "EVENING"}, "slot"),
        ({"date_strategy": "fixed"}, "date_strategy"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"extra": True}, "unsupported"),
    ],
)
def test_configuration_validation(overrides, message):
    with pytest.raises(BCParksConfigurationError, match=message):
        BCParksDayUseDriver().validate_config(make_config(**overrides))


def test_missing_configuration_is_reported():
    config = deepcopy(make_config())
    del config["facility"]

    with pytest.raises(BCParksConfigurationError, match=r"missing.*facility"):
        BCParksDayUseDriver().validate_config(config)
