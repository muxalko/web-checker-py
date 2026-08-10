from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from web_checker.core.models import Availability, CheckResult, Opportunity


def test_opportunity_normalizes_identity_and_freezes_attributes():
    source_attributes = {"capacity": "2"}

    opportunity = Opportunity(
        id=" morning-pass ",
        title=" Morning Pass ",
        availability=Availability.AVAILABLE,
        attributes=source_attributes,
    )
    source_attributes["capacity"] = "99"

    assert opportunity.id == "morning-pass"
    assert opportunity.title == "Morning Pass"
    assert opportunity.attributes == {"capacity": "2"}
    with pytest.raises(TypeError):
        opportunity.attributes["capacity"] = "3"
    with pytest.raises(FrozenInstanceError):
        opportunity.title = "Changed"


@pytest.mark.parametrize("field", ["id", "title"])
def test_opportunity_rejects_blank_required_text(field):
    values = {
        "id": "pass-1",
        "title": "Pass",
        "availability": Availability.AVAILABLE,
    }
    values[field] = "  "

    with pytest.raises(ValueError, match="must not be blank"):
        Opportunity(**values)


def test_opportunity_requires_normalized_availability():
    with pytest.raises(TypeError, match="Availability"):
        Opportunity(id="pass", title="Pass", availability="available")


def test_check_result_requires_aware_timestamp():
    with pytest.raises(ValueError, match="timezone-aware"):
        CheckResult(checked_at=datetime(2026, 8, 22), opportunities=())


def test_check_result_requires_tuple():
    with pytest.raises(TypeError, match="tuple"):
        CheckResult(
            checked_at=datetime.now(UTC),
            opportunities=[],
        )


def test_check_result_rejects_duplicate_ids():
    opportunity = Opportunity(
        id="pass", title="Pass", availability=Availability.AVAILABLE
    )

    with pytest.raises(ValueError, match="duplicate"):
        CheckResult(
            checked_at=datetime.now(UTC),
            opportunities=(opportunity, opportunity),
        )
