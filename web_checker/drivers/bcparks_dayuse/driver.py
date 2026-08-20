"""Read-only driver for the public BC Parks day-use availability API."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from web_checker.core.models import Availability, CheckResult, Opportunity
from web_checker.drivers.base import DriverConfig
from web_checker.drivers.bcparks_dayuse.errors import (
    BCParksConfigurationError,
    BCParksParseError,
    BCParksProviderClosedError,
    BCParksRequestError,
    BCParksResponseError,
    BCParksResponseTooLargeError,
    BCParksTimeoutError,
)
from web_checker.drivers.bcparks_dayuse.models import (
    Facility,
    ProviderConfig,
    ReservationSlot,
)
from web_checker.drivers.bcparks_dayuse.parser import (
    decode_json,
    parse_facilities,
    parse_parks,
    parse_provider_config,
    parse_reservations,
)

DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
DEFAULT_USER_AGENT = "WebChecker/0.1"
PROVIDER_TIMEZONE = ZoneInfo("America/Vancouver")
SUPPORTED_DATE_STRATEGY = "rolling_window"


@dataclass(frozen=True, slots=True)
class BCParksDayUseConfig:
    base_url: str
    park_id: str
    facility: str
    slot: str
    date_strategy: str = SUPPORTED_DATE_STRATEGY
    app_version: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES

    def __post_init__(self) -> None:
        parsed_url = urlsplit(self.base_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise BCParksConfigurationError(
                "base_url must be an absolute HTTP or HTTPS URL"
            )
        if parsed_url.username is not None or parsed_url.password is not None:
            raise BCParksConfigurationError("base_url must not contain credentials")
        if parsed_url.query or parsed_url.fragment:
            raise BCParksConfigurationError(
                "base_url must not contain a query string or fragment"
            )
        if not isinstance(self.park_id, str) or not self.park_id.isdigit():
            raise BCParksConfigurationError("park_id must contain only digits")
        if not isinstance(self.facility, str) or not self.facility.strip():
            raise BCParksConfigurationError("facility must be a non-blank string")
        if self.slot not in {"AM", "PM", "DAY"}:
            raise BCParksConfigurationError("slot must be one of AM, PM, or DAY")
        if self.date_strategy != SUPPORTED_DATE_STRATEGY:
            raise BCParksConfigurationError(
                f"date_strategy must be {SUPPORTED_DATE_STRATEGY!r}"
            )
        if self.app_version is not None and (
            not isinstance(self.app_version, str) or not self.app_version.strip()
        ):
            raise BCParksConfigurationError(
                "app_version must be omitted or a non-blank string"
            )
        if (
            not isinstance(self.timeout_seconds, (int, float))
            or isinstance(self.timeout_seconds, bool)
            or self.timeout_seconds <= 0
            or self.timeout_seconds > 120
        ):
            raise BCParksConfigurationError(
                "timeout_seconds must be greater than 0 and at most 120"
            )
        if (
            not isinstance(self.max_response_bytes, int)
            or isinstance(self.max_response_bytes, bool)
            or self.max_response_bytes <= 0
            or self.max_response_bytes > 2 * 1024 * 1024
        ):
            raise BCParksConfigurationError(
                "max_response_bytes must be between 1 and 2097152"
            )
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "facility", self.facility.strip())
        object.__setattr__(self, "timeout_seconds", float(self.timeout_seconds))
        if self.app_version is not None:
            object.__setattr__(self, "app_version", self.app_version.strip())


class BCParksDayUseDriver:
    """Observes one BC Parks facility and slot across its rolling date window."""

    name = "bcparks_dayuse"

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def validate_config(self, config: DriverConfig) -> None:
        self._parse_config(config)

    async def check(self, config: DriverConfig) -> CheckResult:
        parsed = self._parse_config(config)
        headers = {
            "Accept": "application/json",
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if parsed.app_version is not None:
            headers["X-App-Version"] = parsed.app_version
        timeout = httpx.Timeout(parsed.timeout_seconds)

        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                headers=headers,
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                provider_config = parse_provider_config(
                    await self._get_json(client, parsed, "/api/config")
                )
                parks = parse_parks(await self._get_json(client, parsed, "/api/park"))
                park = next((item for item in parks if item.id == parsed.park_id), None)
                if park is None:
                    raise BCParksParseError(
                        f"Park id {parsed.park_id!r} is absent from the park response"
                    )
                if not park.visible or park.status != "open":
                    raise BCParksProviderClosedError(
                        f"Park {park.name!r} is not open and visible"
                    )

                facilities = parse_facilities(
                    await self._get_json(
                        client,
                        parsed,
                        "/api/facility",
                        params={"park": parsed.park_id, "facilities": "true"},
                    ),
                    park_id=parsed.park_id,
                )
                facility = next(
                    (item for item in facilities if item.name == parsed.facility), None
                )
                if facility is None:
                    raise BCParksParseError(
                        f"Facility {parsed.facility!r} is absent from the facility "
                        "response"
                    )
                if not facility.visible or facility.status != "open":
                    raise BCParksProviderClosedError(
                        f"Facility {facility.name!r} is not open and visible"
                    )
                if parsed.slot not in facility.booking_times:
                    raise BCParksParseError(
                        f"Facility {facility.name!r} does not offer slot "
                        f"{parsed.slot!r}"
                    )
                if (
                    provider_config.advance_booking_hour
                    != facility.booking_opening_hour
                ):
                    raise BCParksParseError(
                        "Provider and facility booking opening hours disagree"
                    )
                if (
                    provider_config.advance_booking_limit
                    != facility.booking_days_ahead + 1
                ):
                    raise BCParksParseError(
                        "Provider and facility booking windows disagree"
                    )

                reservations = parse_reservations(
                    await self._get_json(
                        client,
                        parsed,
                        "/api/reservation",
                        params={
                            "facility": parsed.facility,
                            "park": parsed.park_id,
                        },
                    )
                )
        except httpx.TimeoutException as error:
            raise BCParksTimeoutError(
                f"BC Parks request timed out after {parsed.timeout_seconds:g} seconds"
            ) from error
        except httpx.RequestError as error:
            raise BCParksRequestError(f"BC Parks request failed: {error}") from error

        opportunities = self._build_opportunities(
            parsed,
            park_name=park.name,
            provider_config=provider_config,
            facility=facility,
            reservations=reservations,
        )
        if not opportunities:
            raise BCParksParseError(
                "Reservation response contained no usable opportunities"
            )
        return CheckResult(checked_at=self._clock(), opportunities=opportunities)

    @staticmethod
    async def _get_json(
        client: httpx.AsyncClient,
        config: BCParksDayUseConfig,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> object:
        url = f"{config.base_url}{path}"
        async with client.stream("GET", url, params=params) as response:
            if not response.is_success:
                raise BCParksResponseError(
                    f"BC Parks returned HTTP {response.status_code} for {response.url}"
                )
            content_type = response.headers.get("Content-Type", "")
            if "application/json" not in content_type.lower():
                raise BCParksResponseError(
                    f"BC Parks returned non-JSON content for {response.url}"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > config.max_response_bytes:
                    raise BCParksResponseTooLargeError(
                        "BC Parks response exceeded "
                        f"{config.max_response_bytes} bytes for {response.url}"
                    )
                body.extend(chunk)
        return decode_json(bytes(body), source=str(response.url))

    @classmethod
    def _build_opportunities(
        cls,
        config: BCParksDayUseConfig,
        *,
        park_name: str,
        provider_config: ProviderConfig,
        facility: Facility,
        reservations: tuple[ReservationSlot, ...],
    ) -> tuple[Opportunity, ...]:
        provider_now = facility.current_time.astimezone(PROVIDER_TIMEZONE)
        local_today = provider_now.date()
        latest_date = local_today + timedelta(days=facility.booking_days_ahead)
        expected_dates = {
            local_today + timedelta(days=offset)
            for offset in range(facility.booking_days_ahead + 1)
        }
        response_dates = {item.date for item in reservations}
        if response_dates != expected_dates:
            missing = sorted(expected_dates - response_dates)
            unexpected = sorted(response_dates - expected_dates)
            details = []
            if missing:
                details.append(
                    "missing " + ", ".join(item.isoformat() for item in missing)
                )
            if unexpected:
                details.append(
                    "unexpected " + ", ".join(item.isoformat() for item in unexpected)
                )
            raise BCParksParseError(
                "Reservation response dates do not match the booking window: "
                + "; ".join(details)
            )

        matching = [item for item in reservations if item.slot == config.slot]
        dates = {item.date for item in matching}
        if dates != response_dates:
            missing = sorted(response_dates - dates)
            raise BCParksParseError(
                f"Reservation response is missing slot {config.slot!r} for dates: "
                + ", ".join(item.isoformat() for item in missing)
            )

        pass_limit = (
            provider_config.parking_pass_limit
            if facility.type == "Parking"
            else provider_config.trail_pass_limit
        )
        over_limit = [item for item in matching if item.max_reservable > pass_limit]
        if over_limit:
            raise BCParksParseError(
                "Reservation response exceeds the configured pass limit for "
                f"{facility.type.lower()} facilities"
            )

        opportunities = []
        for item in matching:
            if not local_today <= item.date <= latest_date:
                raise BCParksParseError(
                    f"Reservation date {item.date.isoformat()} is outside the "
                    "facility booking window"
                )
            starts_at, ends_at = cls._slot_window(config.park_id, item.date, item.slot)
            if provider_now >= ends_at:
                continue
            booking_opens = datetime.combine(
                item.date - timedelta(days=facility.booking_days_ahead),
                time(facility.booking_opening_hour),
                tzinfo=PROVIDER_TIMEZONE,
            )
            booking_day = (
                facility.booking_days[item.date.isoweekday()]
                or item.date in facility.bookable_holidays
            )
            if not booking_day:
                availability = Availability.UNKNOWN
                state = "not_required"
            elif provider_now < booking_opens:
                availability = Availability.UNKNOWN
                state = "pre_open"
            elif item.capacity == "Full":
                availability = Availability.UNAVAILABLE
                state = "bookable"
            else:
                availability = Availability.AVAILABLE
                state = "bookable"

            opportunities.append(
                Opportunity(
                    id=(
                        f"{config.park_id}:{facility.name}:"
                        f"{item.date.isoformat()}:{item.slot}"
                    ),
                    title=(
                        f"{park_name} — {facility.name} — {item.slot} "
                        f"on {item.date.isoformat()}"
                    ),
                    availability=availability,
                    starts_at=starts_at,
                    booking_url=f"{config.base_url}/dayuse/",
                    attributes={
                        "park_id": config.park_id,
                        "park": park_name,
                        "facility": facility.name,
                        "facility_type": facility.type,
                        "slot": item.slot,
                        "capacity": item.capacity,
                        "max_reservable": item.max_reservable,
                        "configured_capacity": facility.booking_times[item.slot],
                        "booking_state": state,
                        "provider_time": facility.current_time.isoformat(),
                    },
                )
            )
        return tuple(opportunities)

    @staticmethod
    def _slot_window(
        park_id: str, reservation_date: date, slot: str
    ) -> tuple[datetime, datetime]:
        if park_id == "0015":
            windows = {
                "AM": (time(7), time(12)),
                "PM": (time(12), time(16)),
                "DAY": (time(7), time(16)),
            }
        else:
            windows = {
                "AM": (time(7), time(13)),
                "PM": (time(13), time(15, 30)),
                "DAY": (time(7), time(15, 30)),
            }
        start, end = windows[slot]
        return (
            datetime.combine(reservation_date, start, tzinfo=PROVIDER_TIMEZONE),
            datetime.combine(reservation_date, end, tzinfo=PROVIDER_TIMEZONE),
        )

    @staticmethod
    def _parse_config(config: DriverConfig) -> BCParksDayUseConfig:
        if not isinstance(config, Mapping):
            raise BCParksConfigurationError("driver configuration must be a mapping")
        allowed = {
            "base_url",
            "park_id",
            "facility",
            "slot",
            "date_strategy",
            "app_version",
            "timeout_seconds",
            "max_response_bytes",
        }
        unexpected = set(config) - allowed
        if unexpected:
            raise BCParksConfigurationError(
                "unsupported configuration fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        required = {"base_url", "park_id", "facility", "slot"}
        missing = required - set(config)
        if missing:
            raise BCParksConfigurationError(
                f"missing configuration fields: {', '.join(sorted(missing))}"
            )
        try:
            return BCParksDayUseConfig(
                base_url=config["base_url"],
                park_id=config["park_id"],
                facility=config["facility"],
                slot=config["slot"],
                date_strategy=config.get("date_strategy", SUPPORTED_DATE_STRATEGY),
                app_version=config.get("app_version"),
                timeout_seconds=config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
                max_response_bytes=config.get(
                    "max_response_bytes", DEFAULT_MAX_RESPONSE_BYTES
                ),
            )
        except BCParksConfigurationError:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise BCParksConfigurationError(
                f"invalid BC Parks driver configuration: {error}"
            ) from error
