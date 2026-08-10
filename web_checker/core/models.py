"""Immutable values shared by the checker core and provider drivers."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any


class Availability(StrEnum):
    """Normalized availability understood by the checker core."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Opportunity:
    """One provider opportunity with a stable identity within a check job."""

    id: str
    title: str
    availability: Availability
    starts_at: datetime | None = None
    booking_url: str | None = None
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("Opportunity ID must not be blank")
        if not self.title or not self.title.strip():
            raise ValueError("Opportunity title must not be blank")
        if not isinstance(self.availability, Availability):
            raise TypeError("availability must be an Availability value")
        if self.booking_url is not None and not self.booking_url.strip():
            raise ValueError("Booking URL must be omitted or non-blank")

        object.__setattr__(self, "id", self.id.strip())
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Complete successful observation returned by a driver."""

    checked_at: datetime
    opportunities: tuple[Opportunity, ...]

    def __post_init__(self) -> None:
        if self.checked_at.tzinfo is None or self.checked_at.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        if not isinstance(self.opportunities, tuple):
            raise TypeError("opportunities must be a tuple")

        ids = [opportunity.id for opportunity in self.opportunities]
        if len(ids) != len(set(ids)):
            raise ValueError("Check result contains duplicate opportunity IDs")
