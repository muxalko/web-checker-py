"""Strict parsers for the public BC Parks day-use JSON contract."""

import json
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

from web_checker.drivers.bcparks_dayuse.errors import BCParksParseError
from web_checker.drivers.bcparks_dayuse.models import (
    Facility,
    Park,
    ProviderConfig,
    ReservationSlot,
)

KNOWN_PARK_STATES = {"open", "closed"}
KNOWN_FACILITY_STATES = {"open", "closed"}
KNOWN_FACILITY_TYPES = {"Parking", "Trail"}
KNOWN_SLOTS = {"AM", "PM", "DAY"}
KNOWN_CAPACITY_STATES = {"Full", "Low", "Medium", "High"}


def decode_json(body: bytes, *, source: str) -> Any:
    """Decode UTF-8 JSON while preserving useful provider context."""
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BCParksParseError(f"Invalid JSON from {source}: {error}") from error


def parse_provider_config(value: Any) -> ProviderConfig:
    item = _object(value, "config response")
    return ProviderConfig(
        advance_booking_limit=_integer(
            item.get("ADVANCE_BOOKING_LIMIT"),
            "config ADVANCE_BOOKING_LIMIT",
            minimum=1,
            maximum=31,
        ),
        advance_booking_hour=_integer(
            item.get("ADVANCE_BOOKING_HOUR"),
            "config ADVANCE_BOOKING_HOUR",
            minimum=0,
            maximum=23,
        ),
        parking_pass_limit=_integer(
            item.get("PARKING_PASS_LIMIT"),
            "config PARKING_PASS_LIMIT",
            minimum=1,
            maximum=100,
        ),
        trail_pass_limit=_integer(
            item.get("TRAIL_PASS_LIMIT"),
            "config TRAIL_PASS_LIMIT",
            minimum=1,
            maximum=100,
        ),
    )


def parse_parks(value: Any) -> tuple[Park, ...]:
    items = _non_empty_list(value, "park response")
    parks = []
    seen = set()
    for index, raw_item in enumerate(items):
        label = f"park response item {index}"
        item = _object(raw_item, label)
        park_id = _text(item.get("sk"), f"{label} sk")
        if park_id in seen:
            raise BCParksParseError(f"Duplicate park id {park_id!r}")
        seen.add(park_id)
        if item.get("pk") != "park":
            raise BCParksParseError(f"{label} pk must be 'park'")
        status = _text(item.get("status"), f"{label} status")
        if status not in KNOWN_PARK_STATES:
            raise BCParksParseError(f"{label} has unknown status {status!r}")
        parks.append(
            Park(
                id=park_id,
                name=_text(item.get("name"), f"{label} name"),
                status=status,
                visible=_boolean(item.get("visible"), f"{label} visible"),
            )
        )
    return tuple(parks)


def parse_facilities(value: Any, *, park_id: str) -> tuple[Facility, ...]:
    items = _non_empty_list(value, "facility response")
    facilities = []
    seen = set()
    for index, raw_item in enumerate(items):
        label = f"facility response item {index}"
        item = _object(raw_item, label)
        name = _text(item.get("name"), f"{label} name")
        if name in seen:
            raise BCParksParseError(f"Duplicate facility name {name!r}")
        seen.add(name)
        if item.get("sk") != name:
            raise BCParksParseError(f"{label} sk must match its name")
        if item.get("pk") != f"facility::{park_id}":
            raise BCParksParseError(f"{label} belongs to an unexpected park")
        facility_type = _text(item.get("type"), f"{label} type")
        if facility_type not in KNOWN_FACILITY_TYPES:
            raise BCParksParseError(
                f"{label} has unknown facility type {facility_type!r}"
            )
        status_item = _object(item.get("status"), f"{label} status")
        status = _text(status_item.get("state"), f"{label} status state")
        if status not in KNOWN_FACILITY_STATES:
            raise BCParksParseError(f"{label} has unknown status {status!r}")
        booking_days = _parse_booking_days(item.get("bookingDays"), label)
        booking_times = _parse_booking_times(item.get("bookingTimes"), label)
        holidays = _parse_holidays(item.get("bookableHolidays"), label)
        current_time = _timestamp(item.get("currentTime"), f"{label} currentTime")
        facilities.append(
            Facility(
                park_id=park_id,
                name=name,
                type=facility_type,
                status=status,
                visible=_boolean(item.get("visible"), f"{label} visible"),
                booking_days_ahead=_integer(
                    item.get("bookingDaysAhead"),
                    f"{label} bookingDaysAhead",
                    minimum=0,
                    maximum=31,
                ),
                booking_opening_hour=_integer(
                    item.get("bookingOpeningHour"),
                    f"{label} bookingOpeningHour",
                    minimum=0,
                    maximum=23,
                ),
                booking_days=booking_days,
                booking_times=booking_times,
                bookable_holidays=holidays,
                current_time=current_time,
            )
        )
    return tuple(facilities)


def parse_reservations(value: Any) -> tuple[ReservationSlot, ...]:
    response = _object(value, "reservation response")
    if not response:
        raise BCParksParseError("Reservation response must not be empty")
    reservations = []
    for raw_date, raw_slots in response.items():
        try:
            reservation_date = date.fromisoformat(raw_date)
        except (TypeError, ValueError) as error:
            raise BCParksParseError(
                f"Reservation response has invalid date {raw_date!r}"
            ) from error
        slots = _object(raw_slots, f"reservation date {raw_date}")
        if not slots:
            raise BCParksParseError(
                f"Reservation date {raw_date} must contain at least one slot"
            )
        for slot, raw_availability in slots.items():
            if slot not in KNOWN_SLOTS:
                raise BCParksParseError(
                    f"Reservation date {raw_date} has unknown slot {slot!r}"
                )
            availability = _object(
                raw_availability, f"reservation date {raw_date} slot {slot}"
            )
            capacity = _text(
                availability.get("capacity"),
                f"reservation date {raw_date} slot {slot} capacity",
            )
            if capacity not in KNOWN_CAPACITY_STATES:
                raise BCParksParseError(
                    f"Reservation date {raw_date} slot {slot} has unknown "
                    f"capacity {capacity!r}"
                )
            max_reservable = _integer(
                availability.get("max"),
                f"reservation date {raw_date} slot {slot} max",
                minimum=0,
                maximum=100,
            )
            if (capacity == "Full") != (max_reservable == 0):
                raise BCParksParseError(
                    f"Reservation date {raw_date} slot {slot} has inconsistent "
                    "capacity and max"
                )
            reservations.append(
                ReservationSlot(
                    date=reservation_date,
                    slot=slot,
                    capacity=capacity,
                    max_reservable=max_reservable,
                )
            )
    return tuple(sorted(reservations, key=lambda item: (item.date, item.slot)))


def _parse_booking_days(value: Any, label: str) -> dict[int, bool]:
    raw_days = _object(value, f"{label} bookingDays")
    expected = {str(day) for day in range(1, 8)}
    if set(raw_days) != expected:
        raise BCParksParseError(f"{label} bookingDays must define days 1 through 7")
    return {
        int(day): _boolean(raw_days[day], f"{label} bookingDays {day}")
        for day in sorted(raw_days)
    }


def _parse_booking_times(value: Any, label: str) -> dict[str, int]:
    raw_times = _object(value, f"{label} bookingTimes")
    if not raw_times:
        raise BCParksParseError(f"{label} bookingTimes must not be empty")
    unexpected = set(raw_times) - KNOWN_SLOTS
    if unexpected:
        raise BCParksParseError(
            f"{label} bookingTimes has unknown slots: {', '.join(sorted(unexpected))}"
        )
    return {
        slot: _integer(
            _object(raw_value, f"{label} bookingTimes {slot}").get("max"),
            f"{label} bookingTimes {slot} max",
            minimum=1,
            maximum=100000,
        )
        for slot, raw_value in raw_times.items()
    }


def _parse_holidays(value: Any, label: str) -> frozenset[date]:
    raw_holidays = _object(value, f"{label} bookableHolidays")
    holidays = set()
    for raw_date, enabled in raw_holidays.items():
        if not _boolean(enabled, f"{label} bookableHolidays {raw_date}"):
            continue
        try:
            holidays.add(date.fromisoformat(raw_date))
        except (TypeError, ValueError) as error:
            raise BCParksParseError(
                f"{label} bookableHolidays has invalid date {raw_date!r}"
            ) from error
    return frozenset(holidays)


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise BCParksParseError(f"{label} must be a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise BCParksParseError(f"{label} keys must be strings")
    return value


def _non_empty_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list) or not value:
        raise BCParksParseError(f"{label} must be a non-empty JSON array")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BCParksParseError(f"{label} must be a non-blank string")
    return value.strip()


def _boolean(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise BCParksParseError(f"{label} must be a boolean")
    return value


def _integer(
    value: Any,
    label: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise BCParksParseError(
            f"{label} must be an integer between {minimum} and {maximum}"
        )
    return value


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise BCParksParseError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BCParksParseError(f"{label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BCParksParseError(f"{label} must include a timezone")
    return parsed
