import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from web_checker.drivers.welcomebc_high_impact.driver import (
    DEFAULT_URL,
    WelcomeBCHighImpactDriver,
)
from web_checker.drivers.welcomebc_high_impact.errors import (
    WelcomeBCConfigurationError,
    WelcomeBCParseError,
    WelcomeBCRequestError,
    WelcomeBCResponseError,
    WelcomeBCResponseTooLargeError,
    WelcomeBCTimeoutError,
)

CHECKED_AT = datetime(2026, 8, 19, 5, 0, tzinfo=UTC)


def run_check(driver, config=None):
    return asyncio.run(driver.check({} if config is None else config))


def html_response(content, status=200, headers=None):
    response_headers = {"Content-Type": "text/html; charset=utf-8"}
    response_headers.update(headers or {})
    return httpx.Response(status, text=content, headers=response_headers)


def test_fetches_page_with_one_anonymous_bounded_get(welcomebc_fixture):
    observed = []

    def handler(request):
        observed.append(request)
        return html_response(welcomebc_fixture("invitations.html"))

    driver = WelcomeBCHighImpactDriver(
        transport=httpx.MockTransport(handler), clock=lambda: CHECKED_AT
    )

    result = run_check(driver)

    assert result.checked_at == CHECKED_AT
    assert len(result.opportunities) == 2
    assert len(observed) == 1
    assert observed[0].method == "GET"
    assert str(observed[0].url) == DEFAULT_URL
    assert observed[0].headers["User-Agent"] == ("WebChecker/0.1 WelcomeBCHighImpact")
    assert observed[0].headers["Accept"] == "text/html,application/xhtml+xml"
    assert "authorization" not in observed[0].headers
    assert "cookie" not in observed[0].headers


def test_final_redirect_url_is_used_as_source_link(welcomebc_fixture):
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/final"})
        return html_response(welcomebc_fixture("invitations.html"))

    driver = WelcomeBCHighImpactDriver(transport=httpx.MockTransport(handler))

    result = run_check(driver, {"url": "https://provider.test/start"})

    assert all(
        item.booking_url == "https://provider.test/final"
        for item in result.opportunities
    )


def test_non_success_status_is_rejected():
    driver = WelcomeBCHighImpactDriver(
        transport=httpx.MockTransport(lambda request: httpx.Response(503))
    )

    with pytest.raises(WelcomeBCResponseError, match="HTTP 503"):
        run_check(driver)


@pytest.mark.parametrize("content_type", ["application/json", "", "text/plain"])
def test_non_html_response_is_rejected(content_type):
    headers = {"Content-Type": content_type} if content_type else {}
    driver = WelcomeBCHighImpactDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="blocked", headers=headers)
        )
    )

    with pytest.raises(WelcomeBCResponseError, match="non-HTML"):
        run_check(driver)


def test_timeout_has_specific_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    driver = WelcomeBCHighImpactDriver(transport=httpx.MockTransport(handler))

    with pytest.raises(WelcomeBCTimeoutError, match="timed out after 2 seconds"):
        run_check(driver, {"timeout_seconds": 2})


def test_other_request_failure_is_wrapped():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    driver = WelcomeBCHighImpactDriver(transport=httpx.MockTransport(handler))

    with pytest.raises(WelcomeBCRequestError, match="connection refused"):
        run_check(driver)


def test_response_size_is_bounded():
    driver = WelcomeBCHighImpactDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"x" * 11,
                headers={"Content-Type": "text/html"},
            )
        )
    )

    with pytest.raises(WelcomeBCResponseTooLargeError, match="exceeded 10 bytes"):
        run_check(driver, {"max_response_bytes": 10})


def test_parser_failure_is_wrapped_with_url():
    driver = WelcomeBCHighImpactDriver(
        transport=httpx.MockTransport(
            lambda request: html_response("<html><body>changed</body></html>")
        )
    )

    with pytest.raises(WelcomeBCParseError, match="Could not safely parse") as error:
        run_check(driver)

    assert DEFAULT_URL in str(error.value)
    assert error.value.__cause__ is not None


@pytest.mark.parametrize(
    "config, message",
    [
        ({"url": "ftp://provider.test/page"}, "absolute HTTP or HTTPS"),
        ({"url": "https://user:secret@provider.test/page"}, "credentials"),
        ({"url": 123}, "url must be a string"),
        ({"timeout_seconds": 0}, "timeout_seconds"),
        ({"timeout_seconds": 121}, "timeout_seconds"),
        ({"timeout_seconds": True}, "timeout_seconds"),
        ({"max_response_bytes": 0}, "max_response_bytes"),
        ({"max_response_bytes": 20 * 1024 * 1024 + 1}, "max_response_bytes"),
        ({"max_response_bytes": True}, "max_response_bytes"),
        ({"extra": True}, "unsupported configuration fields"),
    ],
)
def test_configuration_validation(config, message):
    with pytest.raises(WelcomeBCConfigurationError, match=message):
        WelcomeBCHighImpactDriver().validate_config(config)


def test_empty_configuration_uses_official_defaults():
    WelcomeBCHighImpactDriver().validate_config({})
