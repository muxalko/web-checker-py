"""Validated provider-specific values used by the BC Parks driver."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    advance_booking_limit: int
    advance_booking_hour: int
    parking_pass_limit: int
    trail_pass_limit: int


@dataclass(frozen=True, slots=True)
class Park:
    id: str
    name: str
    status: str
    visible: bool


@dataclass(frozen=True, slots=True)
class Facility:
    park_id: str
    name: str
    type: str
    status: str
    visible: bool
    booking_days_ahead: int
    booking_opening_hour: int
    booking_days: Mapping[int, bool]
    booking_times: Mapping[str, int]
    bookable_holidays: frozenset[date]
    current_time: datetime


@dataclass(frozen=True, slots=True)
class ReservationSlot:
    date: date
    slot: str
    capacity: str
    max_reservable: int
