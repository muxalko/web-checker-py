"""Asynchronous HTTP driver for declaratively structured HTML pages."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx

from web_checker.core.models import CheckResult
from web_checker.drivers.base import DriverConfig
from web_checker.drivers.generic_html.errors import (
    DriverConfigurationError,
    DriverParseError,
    DriverRequestError,
    DriverResponseError,
    DriverTimeoutError,
    HtmlParserError,
    ParserConfigurationError,
    ResponseTooLargeError,
)
from web_checker.drivers.generic_html.parser import GenericHtmlParser, ParserConfig

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
DEFAULT_USER_AGENT = "WebChecker/0.1"


@dataclass(frozen=True, slots=True)
class GenericHtmlDriverConfig:
    """Validated provider and extraction configuration."""

    url: str
    parser: ParserConfig
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed_url = urlsplit(self.url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise DriverConfigurationError("url must be an absolute HTTP or HTTPS URL")
        if parsed_url.username is not None or parsed_url.password is not None:
            raise DriverConfigurationError("url must not contain credentials")
        if not isinstance(self.parser, ParserConfig):
            raise DriverConfigurationError("parser must be a ParserConfig")
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 120
        ):
            raise DriverConfigurationError(
                "timeout_seconds must be greater than 0 and at most 120"
            )
        if (
            not isinstance(self.max_response_bytes, int)
            or isinstance(self.max_response_bytes, bool)
            or self.max_response_bytes <= 0
            or self.max_response_bytes > 20 * 1024 * 1024
        ):
            raise DriverConfigurationError(
                "max_response_bytes must be between 1 and 20971520"
            )

        normalized_headers: dict[str, str] = {}
        for name, value in self.headers.items():
            if not isinstance(name, str) or not name.strip():
                raise DriverConfigurationError("header names must be non-blank strings")
            if not isinstance(value, str) or not value.strip():
                raise DriverConfigurationError(
                    f"header {name!r} must have a non-blank string value"
                )
            normalized_headers[name.strip()] = value.strip()

        object.__setattr__(self, "headers", MappingProxyType(normalized_headers))
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))


class GenericHtmlDriver:
    """Fetches a bounded HTML response and delegates interpretation to the parser."""

    name = "generic_html"

    def __init__(
        self,
        *,
        parser: GenericHtmlParser | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._parser = parser or GenericHtmlParser()
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def validate_config(self, config: DriverConfig) -> None:
        self._parse_config(config)

    async def check(self, config: DriverConfig) -> CheckResult:
        parsed_config = self._parse_config(config)
        headers = {"User-Agent": DEFAULT_USER_AGENT, **parsed_config.headers}
        timeout = httpx.Timeout(parsed_config.timeout_seconds)

        try:
            async with (
                httpx.AsyncClient(
                    transport=self._transport,
                    headers=headers,
                    timeout=timeout,
                    follow_redirects=True,
                ) as client,
                client.stream("GET", parsed_config.url) as response,
            ):
                if not response.is_success:
                    raise DriverResponseError(
                        f"Provider returned HTTP {response.status_code} for "
                        f"{parsed_config.url}"
                    )
                body = await self._read_bounded(response, parsed_config)
                response_url = str(response.url)
                encoding = response.encoding or "utf-8"
        except httpx.TimeoutException as error:
            raise DriverTimeoutError(
                f"Provider request timed out after "
                f"{parsed_config.timeout_seconds:g} seconds"
            ) from error
        except httpx.RequestError as error:
            raise DriverRequestError(
                f"Provider request failed for {parsed_config.url}: {error}"
            ) from error

        html = body.decode(encoding, errors="replace")
        try:
            opportunities = self._parser.parse(
                html, base_url=response_url, config=parsed_config.parser
            )
        except HtmlParserError as error:
            raise DriverParseError(
                f"Could not safely parse provider response from {response_url}: {error}"
            ) from error

        return CheckResult(
            checked_at=self._clock(),
            opportunities=opportunities,
        )

    @staticmethod
    async def _read_bounded(
        response: httpx.Response, config: GenericHtmlDriverConfig
    ) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > config.max_response_bytes:
                raise ResponseTooLargeError(
                    f"Provider response exceeded {config.max_response_bytes} bytes"
                )
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    def _parse_config(config: DriverConfig) -> GenericHtmlDriverConfig:
        if not isinstance(config, Mapping):
            raise DriverConfigurationError("driver configuration must be a mapping")

        allowed = {
            "url",
            "parser",
            "headers",
            "timeout_seconds",
            "max_response_bytes",
        }
        unexpected = set(config) - allowed
        if unexpected:
            raise DriverConfigurationError(
                "unsupported configuration fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        missing = {"url", "parser"} - set(config)
        if missing:
            raise DriverConfigurationError(
                f"missing configuration fields: {', '.join(sorted(missing))}"
            )

        try:
            raw_parser = config["parser"]
            parser = (
                raw_parser
                if isinstance(raw_parser, ParserConfig)
                else ParserConfig.from_mapping(raw_parser)
            )
            return GenericHtmlDriverConfig(
                url=config["url"],
                parser=parser,
                headers=config.get("headers", {}),
                timeout_seconds=config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
                max_response_bytes=config.get(
                    "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
                ),
            )
        except DriverConfigurationError:
            raise
        except ParserConfigurationError as error:
            raise DriverConfigurationError(
                f"invalid parser configuration: {error}"
            ) from error
        except (AttributeError, TypeError, ValueError) as error:
            raise DriverConfigurationError(
                f"invalid generic HTML driver configuration: {error}"
            ) from error
