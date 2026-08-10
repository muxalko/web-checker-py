"""Application service coordinating drivers and successful state recording."""

from dataclasses import dataclass, field
from typing import Protocol

from web_checker.config import JobDefinition
from web_checker.core.models import CheckResult
from web_checker.core.transitions import OpportunityTransition
from web_checker.drivers.registry import DriverRegistry
from web_checker.notifications.models import DeliverySummary
from web_checker.notifications.service import NotificationService


@dataclass(frozen=True, slots=True)
class RecordOutcome:
    """Provider-independent result of recording a successful snapshot."""

    baseline_created: bool
    transitions: tuple[OpportunityTransition, ...]


class ObservationStore(Protocol):
    """Persistence boundary required by the check service."""

    def record_success(
        self, job_id: str, result: CheckResult, notification_plan
    ) -> RecordOutcome: ...


@dataclass(frozen=True, slots=True)
class CheckExecution:
    """Successful driver result together with persisted comparison outcome."""

    result: CheckResult
    baseline_created: bool
    transitions: tuple[OpportunityTransition, ...]
    delivery: DeliverySummary = field(default_factory=DeliverySummary)


class CheckService:
    """Runs one configured driver and records only complete successful results."""

    def __init__(
        self,
        registry: DriverRegistry,
        store: ObservationStore,
        notification_service: NotificationService | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._notification_service = notification_service

    async def check(self, job: JobDefinition) -> CheckExecution:
        driver = self._registry.get(job.driver)
        result = await driver.check(job.config)
        outcome = self._store.record_success(job.id, result, job.notifications)
        delivery = (
            await self._notification_service.dispatch_pending()
            if self._notification_service is not None
            else DeliverySummary()
        )
        return CheckExecution(
            result=result,
            baseline_created=outcome.baseline_created,
            transitions=outcome.transitions,
            delivery=delivery,
        )
