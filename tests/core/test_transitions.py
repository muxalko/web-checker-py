from datetime import UTC, datetime

from web_checker.core.models import Availability, CheckResult, Opportunity
from web_checker.core.transitions import TransitionType, detect_transitions


def opportunity(identifier, availability, title=None):
    return Opportunity(
        id=identifier,
        title=title or identifier,
        availability=availability,
    )


def result(*opportunities, minute=0):
    return CheckResult(
        checked_at=datetime(2026, 8, 10, 12, minute, tzinfo=UTC),
        opportunities=opportunities,
    )


def test_detects_all_normalized_transition_types_in_deterministic_order():
    previous = result(
        opportunity("available", Availability.UNAVAILABLE),
        opportunity("unavailable", Availability.AVAILABLE),
        opportunity("unknown", Availability.AVAILABLE),
        opportunity("unchanged", Availability.AVAILABLE),
        opportunity("removed", Availability.UNAVAILABLE),
    )
    current = result(
        opportunity("available", Availability.AVAILABLE),
        opportunity("unavailable", Availability.UNAVAILABLE),
        opportunity("unknown", Availability.UNKNOWN),
        opportunity("unchanged", Availability.AVAILABLE, title="Renamed"),
        opportunity("added", Availability.AVAILABLE),
        minute=1,
    )

    transitions = detect_transitions(previous, current)

    assert [(item.opportunity_id, item.type) for item in transitions] == [
        ("available", TransitionType.BECAME_AVAILABLE),
        ("unavailable", TransitionType.BECAME_UNAVAILABLE),
        ("unknown", TransitionType.AVAILABILITY_UNKNOWN),
        ("added", TransitionType.APPEARED),
        ("removed", TransitionType.DISAPPEARED),
    ]
    assert transitions[3].previous is None
    assert transitions[4].current is None


def test_unchanged_availability_produces_no_transition():
    previous = result(opportunity("pass", Availability.AVAILABLE))
    current = result(
        opportunity("pass", Availability.AVAILABLE, title="New title"), minute=1
    )

    assert detect_transitions(previous, current) == ()
