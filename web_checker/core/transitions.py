"""Pure comparison of complete successful opportunity snapshots."""

from dataclasses import dataclass
from enum import StrEnum

from web_checker.core.models import Availability, CheckResult, Opportunity


class TransitionType(StrEnum):
    """Meaningful changes between two complete successful checks."""

    APPEARED = "appeared"
    DISAPPEARED = "disappeared"
    BECAME_AVAILABLE = "became_available"
    BECAME_UNAVAILABLE = "became_unavailable"
    AVAILABILITY_UNKNOWN = "availability_unknown"


@dataclass(frozen=True, slots=True)
class OpportunityTransition:
    """One normalized opportunity change."""

    type: TransitionType
    opportunity_id: str
    previous: Opportunity | None
    current: Opportunity | None

    def __post_init__(self) -> None:
        if self.previous is None and self.current is None:
            raise ValueError("A transition must have previous or current state")
        expected_id = self.current.id if self.current is not None else self.previous.id
        if self.opportunity_id != expected_id:
            raise ValueError("Transition opportunity ID does not match its state")


def detect_transitions(
    previous: CheckResult, current: CheckResult
) -> tuple[OpportunityTransition, ...]:
    """Return deterministic availability transitions between two snapshots."""
    previous_by_id = {item.id: item for item in previous.opportunities}
    current_by_id = {item.id: item for item in current.opportunities}
    transitions: list[OpportunityTransition] = []

    for opportunity in current.opportunities:
        old = previous_by_id.get(opportunity.id)
        if old is None:
            transitions.append(
                OpportunityTransition(
                    type=TransitionType.APPEARED,
                    opportunity_id=opportunity.id,
                    previous=None,
                    current=opportunity,
                )
            )
            continue
        if old.availability == opportunity.availability:
            continue
        transitions.append(
            OpportunityTransition(
                type=_availability_transition(opportunity.availability),
                opportunity_id=opportunity.id,
                previous=old,
                current=opportunity,
            )
        )

    for opportunity in previous.opportunities:
        if opportunity.id not in current_by_id:
            transitions.append(
                OpportunityTransition(
                    type=TransitionType.DISAPPEARED,
                    opportunity_id=opportunity.id,
                    previous=opportunity,
                    current=None,
                )
            )

    return tuple(transitions)


def _availability_transition(availability: Availability) -> TransitionType:
    if availability is Availability.AVAILABLE:
        return TransitionType.BECAME_AVAILABLE
    if availability is Availability.UNAVAILABLE:
        return TransitionType.BECAME_UNAVAILABLE
    return TransitionType.AVAILABILITY_UNKNOWN
