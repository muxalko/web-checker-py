from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from web_checker.core.models import Availability
from web_checker.drivers.welcomebc_high_impact.errors import (
    WelcomeBCInvalidDrawError,
    WelcomeBCUnexpectedPageError,
)
from web_checker.drivers.welcomebc_high_impact.parser import (
    WelcomeBCHighImpactParser,
)

SOURCE_URL = "https://www.welcomebc.ca/invitations-to-apply"


def parse(html):
    return WelcomeBCHighImpactParser().parse(html, SOURCE_URL)


def test_groups_table_routes_and_parses_narrative_draws(welcomebc_fixture):
    opportunities = parse(welcomebc_fixture("invitations.html"))

    assert [item.id for item in opportunities] == [
        "high-economic-impact-2026-07-16",
        "high-economic-impact-2026-04-22",
    ]
    current, narrative = opportunities
    assert current.availability is Availability.AVAILABLE
    assert current.title == (
        "BC PNP High Economic Impact draw on July 16, 2026 (569 invitations)"
    )
    assert current.starts_at == datetime(
        2026, 7, 16, tzinfo=ZoneInfo("America/Vancouver")
    )
    assert current.booking_url == SOURCE_URL
    assert current.attributes["total_invitations"] == 569
    assert current.attributes["selection_routes"] == (
        {
            "selection_factors": (
                "Minimum wage of $58/hour and $115,000/year, and NOC 0, 1, 2, or 3"
            ),
            "minimum_score": "N/A",
            "invitations": "223",
        },
        {
            "selection_factors": "Points",
            "minimum_score": "132",
            "invitations": "346",
        },
    )
    assert narrative.attributes["total_invitations"] == 484
    assert narrative.attributes["selection_routes"][1]["minimum_score"] == "138"


def test_table_entry_wins_when_same_date_also_appears_in_narrative(
    welcomebc_fixture,
):
    html = welcomebc_fixture("invitations.html").replace(
        "<h3>Skills Immigration registration pool</h3>",
        "<h3>July 16, 2026</h3>"
        "<p>On July 16, 2026, the BC PNP issued invitations to apply to "
        "569 candidates who will create high economic impact in B.C.</p>"
        "<ul><li>Points (569 candidates).</li></ul>"
        "<h3>Skills Immigration registration pool</h3>",
    )

    opportunities = parse(html)

    assert [item.id for item in opportunities].count(
        "high-economic-impact-2026-07-16"
    ) == 1
    assert (
        opportunities[0]
        .attributes["selection_routes"][0]["selection_factors"]
        .startswith("Minimum wage")
    )


@pytest.mark.parametrize(
    "old, new, error, message",
    [
        (
            'id="Skills_Immigration_invitations"',
            'id="changed-section"',
            WelcomeBCUnexpectedPageError,
            "section is missing",
        ),
        (
            "Number of invitations",
            "Invitations sent",
            WelcomeBCUnexpectedPageError,
            "headers changed",
        ),
        (
            "July 16, 2026",
            "not-a-date",
            WelcomeBCInvalidDrawError,
            "Invalid draw date",
        ),
        (
            ">223<",
            ">many<",
            WelcomeBCInvalidDrawError,
            "Invalid invitation count",
        ),
        (
            "apply to 484 candidates",
            "apply to 485 candidates",
            WelcomeBCInvalidDrawError,
            "sum to 484, expected 485",
        ),
    ],
)
def test_rejects_empty_or_changed_provider_structure(
    welcomebc_fixture, old, new, error, message
):
    html = welcomebc_fixture("invitations.html").replace(old, new)

    with pytest.raises(error, match=message):
        parse(html)


def test_rejects_page_without_any_high_impact_draws(welcomebc_fixture):
    html = (
        welcomebc_fixture("invitations.html")
        .replace("High Economic Impact", "Other Impact")
        .replace("high economic impact", "other impact")
    )

    with pytest.raises(WelcomeBCUnexpectedPageError, match="No High Economic Impact"):
        parse(html)


def test_rejects_duplicate_selection_route(welcomebc_fixture):
    html = (
        welcomebc_fixture("invitations.html")
        .replace(
            "<td>Points</td>",
            "<td>Minimum wage of $58/hour and $115,000/year, "
            "and NOC 0, 1, 2, or 3</td>",
        )
        .replace("<td>132</td>", "<td>N/A</td>")
    )

    with pytest.raises(WelcomeBCInvalidDrawError, match="duplicate selection routes"):
        parse(html)


def test_rejects_orphan_route_outside_declared_date_rowspan(welcomebc_fixture):
    html = welcomebc_fixture("invitations.html").replace(' rowspan="2"', "", 1)

    with pytest.raises(WelcomeBCInvalidDrawError, match="no valid draw date"):
        parse(html)
