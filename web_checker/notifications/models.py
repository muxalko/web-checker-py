"""Provider-independent notification values."""

from dataclasses import dataclass
from datetime import datetime

from web_checker.core.models import Availability
from web_checker.core.transitions import TransitionType

MAX_DIGEST_ITEMS = 25


@dataclass(frozen=True, slots=True)
class NotificationPlan:
    """Channels and transition types selected for one job."""

    channels: tuple[str, ...] = ()
    on: frozenset[TransitionType] = frozenset()

    def __post_init__(self) -> None:
        if len(self.channels) != len(set(self.channels)):
            raise ValueError("notification channels must be unique")
        if any(not channel.strip() for channel in self.channels):
            raise ValueError("notification channels must not be blank")

    @property
    def enabled(self) -> bool:
        return bool(self.channels and self.on)


@dataclass(frozen=True, slots=True)
class NotificationItem:
    """One provider-independent opportunity change within a digest."""

    transition_type: TransitionType
    opportunity_id: str
    opportunity_title: str
    current_availability: Availability | None
    starts_at: datetime | None
    booking_url: str | None


@dataclass(frozen=True, slots=True)
class PendingNotification:
    """One durable, bounded digest ready for a delivery adapter."""

    id: int
    channel: str
    job_id: str
    checked_at: datetime
    items: tuple[NotificationItem, ...]
    part_number: int
    part_count: int
    attempts: int

    def __post_init__(self) -> None:
        if not self.items:
            raise ValueError("notification digest must contain at least one item")
        if len(self.items) > MAX_DIGEST_ITEMS:
            raise ValueError(
                f"notification digest cannot exceed {MAX_DIGEST_ITEMS} items"
            )
        if self.part_count < 1:
            raise ValueError("notification digest part count must be positive")
        if not 1 <= self.part_number <= self.part_count:
            raise ValueError("notification digest part number is out of range")


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    """Aggregate result of one outbox dispatch pass."""

    delivered: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class OperationalAlert:
    """Provider-independent operational failure awaiting delivery."""

    id: int
    channel: str
    key: str
    job_id: str | None
    title: str
    detail: str
    created_at: datetime
    attempts: int
