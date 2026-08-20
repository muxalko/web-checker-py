"""Bounded HTTP driver for WelcomeBC High Economic Impact ITA draws."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from web_checker.core.models import CheckResult
from web_checker.drivers.base import DriverConfig
from web_checker.drivers.welcomebc_high_impact.errors import (
    WelcomeBCConfigurationError,
    WelcomeBCParseError,
    WelcomeBCParserError,
    WelcomeBCRequestError,
    WelcomeBCResponseError,
    WelcomeBCResponseTooLargeError,
    WelcomeBCTimeoutError,
)
from web_checker.drivers.welcomebc_high_impact.parser import (
    WelcomeBCHighImpactParser,
)

DEFAULT_URL = (
    "https://www.welcomebc.ca/immigrate-to-b-c/"
    "about-the-bc-provincial-nominee-program/invitations-to-apply"
)
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 1024 * 1024
DEFAULT_USER_AGENT = "WebChecker/0.1 WelcomeBCHighImpact"


@dataclass(frozen=True, slots=True)
class WelcomeBCHighImpactConfig:
    """Validated WelcomeBC request configuration."""

    url: str = DEFAULT_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        if not isinstance(self.url, str):
            raise WelcomeBCConfigurationError("url must be a string")
        parsed_url = urlsplit(self.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise WelcomeBCConfigurationError(
                "url must be an absolute HTTP or HTTPS URL"
            )
        if parsed_url.username is not None or parsed_url.password is not None:
            raise WelcomeBCConfigurationError("url must not contain credentials")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 120
        ):
            raise WelcomeBCConfigurationError(
                "timeout_seconds must be greater than 0 and at most 120"
            )
        if (
            not isinstance(self.max_response_bytes, int)
            or isinstance(self.max_response_bytes, bool)
            or self.max_response_bytes <= 0
            or self.max_response_bytes > 20 * 1024 * 1024
        ):
            raise WelcomeBCConfigurationError(
                "max_response_bytes must be between 1 and 20971520"
            )

        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


class WelcomeBCHighImpactDriver:
    """Fetches and normalizes published WelcomeBC High Economic Impact draws."""

    name = "welcomebc_high_impact"

    def __init__(
        self,
        *,
        parser: WelcomeBCHighImpactParser | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._parser = parser or WelcomeBCHighImpactParser()
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def validate_config(self, config: DriverConfig) -> None:
        self._parse_config(config)

    async def check(self, config: DriverConfig) -> CheckResult:
        parsed_config = self._parse_config(config)
        timeout = httpx.Timeout(parsed_config.timeout_seconds)

        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    headers={
                        "Accept": "text/html,application/xhtml+xml",
                        "User-Agent": DEFAULT_USER_AGENT,
                    },
                    timeout=timeout,
                    follow_redirects=True,
                ) as client,
                client.stream("GET", parsed_config.url) as response,
            ):
                if not response.is_success:
                    raise WelcomeBCResponseError(
                        f"WelcomeBC returned HTTP {response.status_code} for "
                        f"{parsed_config.url}"
                    )
                content_type = response.headers.get("Content-Type", "")
                if content_type.split(";", 1)[0].strip().casefold() != "text/html":
                    raise WelcomeBCResponseError(
                        "WelcomeBC returned a non-HTML response "
                        f"({content_type or 'missing Content-Type'})"
                    )
                body = await self._read_bounded(response, parsed_config)
                response_url = str(response.url)
                encoding = response.encoding or "utf-8"
        except httpx.TimeoutException as error:
            raise WelcomeBCTimeoutError(
                "WelcomeBC request timed out after "
                f"{parsed_config.timeout_seconds:g} seconds"
            ) from error
        except httpx.RequestError as error:
            raise WelcomeBCRequestError(
                f"WelcomeBC request failed for {parsed_config.url}: {error}"
            ) from error

        html = body.decode(encoding, errors="replace")
        try:
            opportunities = self._parser.parse(html, response_url)
        except WelcomeBCParserError as error:
            raise WelcomeBCParseError(
                f"Could not safely parse WelcomeBC response from {response_url}: "
                f"{error}"
            ) from error

        return CheckResult(
            checked_at=self._clock(),
            opportunities=opportunities,
        )

    @staticmethod
    async def _read_bounded(
        response: httpx.Response, config: WelcomeBCHighImpactConfig
    ) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > config.max_response_bytes:
                raise WelcomeBCResponseTooLargeError(
                    f"WelcomeBC response exceeded {config.max_response_bytes} bytes"
                )
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _parse_config(config: DriverConfig) -> WelcomeBCHighImpactConfig:
        if not isinstance(config, Mapping):
            raise WelcomeBCConfigurationError("driver configuration must be a mapping")
        allowed = {"url", "timeout_seconds", "max_response_bytes"}
        unexpected = set(config) - allowed
        if unexpected:
            raise WelcomeBCConfigurationError(
                "unsupported configuration fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        try:
            return WelcomeBCHighImpactConfig(
                url=config.get("url", DEFAULT_URL),
                timeout_seconds=config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
                max_response_bytes=config.get(
                    "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
                ),
            )
        except WelcomeBCConfigurationError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise WelcomeBCConfigurationError(
                f"invalid WelcomeBC driver configuration: {error}"
            ) from error
