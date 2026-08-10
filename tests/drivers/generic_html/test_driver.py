import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from web_checker.core.models import Availability
from web_checker.drivers.generic_html.driver import GenericHtmlDriver
from web_checker.drivers.generic_html.errors import (
    DriverConfigurationError,
    DriverParseError,
    DriverRequestError,
    DriverResponseError,
    DriverTimeoutError,
    ResponseTooLargeError,
)

URL = "http://provider.test/reservations/2026-08-22"
CHECKED_AT = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def run_check(driver, config):
    return asyncio.run(driver.check(config))


def make_config(parser_config, **overrides):
    config = {"url": URL, "parser": parser_config}
    config.update(overrides)
    return config


def test_successfully_fetches_and_parses_page(fixture_html, parser_config):
    observed_request = None

    def handler(request):
        nonlocal observed_request
        observed_request = request
        return httpx.Response(
            200,
            text=fixture_html("reservations.html"),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

    driver = GenericHtmlDriver(
        transport=httpx.MockTransport(handler), clock=lambda: CHECKED_AT
    )

    result = run_check(
        driver,
        make_config(parser_config, headers={"X-Test-Header": "fixture"}),
    )

    assert result.checked_at == CHECKED_AT
    assert len(result.opportunities) == 3
    assert result.opportunities[0].availability is Availability.AVAILABLE
    assert observed_request.headers["User-Agent"] == "WebChecker/0.1"
    assert observed_request.headers["X-Test-Header"] == "fixture"


def test_final_redirect_url_resolves_relative_booking_links(
    fixture_html, parser_config
):
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final/page"})
        return httpx.Response(200, text=fixture_html("reservations.html"))

    driver = GenericHtmlDriver(
        transport=httpx.MockTransport(handler), clock=lambda: CHECKED_AT
    )

    result = run_check(
        driver,
        make_config(parser_config, url="http://provider.test/start"),
    )

    assert (
        result.opportunities[0].booking_url == "http://provider.test/book/morning-pass"
    )


def test_non_success_status_is_rejected(parser_config):
    driver = GenericHtmlDriver(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )

    with pytest.raises(DriverResponseError, match="HTTP 503"):
        run_check(driver, make_config(parser_config))


def test_timeout_has_specific_error(parser_config):
    def handler(request):
        raise httpx.ReadTimeout("too slow", request=request)

    driver = GenericHtmlDriver(transport=httpx.MockTransport(handler))

    with pytest.raises(DriverTimeoutError, match="timed out after 2 seconds"):
        run_check(driver, make_config(parser_config, timeout_seconds=2))


def test_other_request_failure_is_wrapped(parser_config):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    driver = GenericHtmlDriver(transport=httpx.MockTransport(handler))

    with pytest.raises(DriverRequestError, match="connection refused"):
        run_check(driver, make_config(parser_config))


def test_response_size_limit_is_enforced_while_reading(parser_config):
    driver = GenericHtmlDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"x" * 11)
        )
    )

    with pytest.raises(ResponseTooLargeError, match="exceeded 10 bytes"):
        run_check(
            driver,
            make_config(parser_config, max_response_bytes=10),
        )


def test_parser_failure_is_wrapped_with_url(fixture_html, parser_config):
    driver = GenericHtmlDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=fixture_html("malformed.html"))
        )
    )

    with pytest.raises(DriverParseError, match="Could not safely parse") as error:
        run_check(driver, make_config(parser_config))

    assert URL in str(error.value)
    assert error.value.__cause__ is not None


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"url": "ftp://provider.test/page"}, "absolute HTTP or HTTPS"),
        ({"url": "http://user:secret@provider.test/page"}, "credentials"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"max_response_bytes": True}, "max_response_bytes"),
        ({"headers": {"X-Test": ""}}, "non-blank string value"),
        ({"extra": "unsupported"}, "unsupported configuration fields"),
    ],
)
def test_configuration_validation(parser_config, overrides, message):
    driver = GenericHtmlDriver()

    with pytest.raises(DriverConfigurationError, match=message):
        driver.validate_config(make_config(parser_config, **overrides))


def test_required_configuration_is_reported(parser_config):
    with pytest.raises(DriverConfigurationError, match=r"missing.*url"):
        GenericHtmlDriver().validate_config({"parser": parser_config})


def test_declarative_parser_configuration_is_converted(parser_config):
    declarative = {
        "opportunity_selector": "[data-opportunity-id]",
        "id": {"attribute": "data-opportunity-id"},
        "title": {"selector": ".title"},
        "availability": {"selector": ".status", "attribute": "data-status"},
        "availability_mapping": {"available": "available"},
    }

    GenericHtmlDriver().validate_config({"url": URL, "parser": declarative})


def test_invalid_declarative_parser_configuration_is_wrapped():
    with pytest.raises(DriverConfigurationError, match="invalid parser configuration"):
        GenericHtmlDriver().validate_config({"url": URL, "parser": {}})
