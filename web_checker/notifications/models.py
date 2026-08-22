"""Provider-independent notification values."""

from dataclasses import dataclass
from datetime import datetime

from web_checker.core.models import Availability
from web_checker.core.transitions import TransitionType


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
class PendingNotification:
    """One durable outbox item ready for a delivery adapter."""

    id: int
    channel: str
    job_id: str
    transition_type: TransitionType
    opportunity_id: str
    opportunity_title: str
    current_availability: Availability | None
    booking_url: str | None
    checked_at: datetime
    attempts: int


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
