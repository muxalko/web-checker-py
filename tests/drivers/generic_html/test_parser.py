from datetime import datetime

import pytest

from web_checker.core.models import Availability
from web_checker.drivers.generic_html.errors import (
    InvalidOpportunityError,
    ParserConfigurationError,
    UnexpectedPageError,
)
from web_checker.drivers.generic_html.parser import (
    GenericHtmlParser,
    ParserConfig,
    ValueSource,
)

BASE_URL = "http://mock-site:8080/reservations/2026-08-22"


def test_parses_normalized_opportunities(fixture_html, parser_config):
    opportunities = GenericHtmlParser().parse(
        fixture_html("reservations.html"), BASE_URL, parser_config
    )

    assert len(opportunities) == 3
    morning, midday, afternoon = opportunities
    assert morning.id == "2026-08-22-morning-pass"
    assert morning.title == "Morning Adventure Pass"
    assert morning.availability is Availability.AVAILABLE
    assert morning.starts_at == datetime(2026, 8, 22, 9, 0)
    assert morning.booking_url == "http://mock-site:8080/book/morning-pass"
    assert morning.attributes == {"capacity": "4"}
    assert midday.availability is Availability.UNAVAILABLE
    assert midday.booking_url is None
    assert afternoon.availability is Availability.UNKNOWN


def test_normalizes_whitespace(fixture_html, parser_config):
    opportunities = GenericHtmlParser().parse(
        fixture_html("reservations.html"), BASE_URL, parser_config
    )

    assert opportunities[0].title == "Morning Adventure Pass"


def test_optional_elements_may_be_absent(fixture_html, parser_config):
    opportunities = GenericHtmlParser().parse(
        fixture_html("missing_optional.html"), BASE_URL, parser_config
    )

    assert opportunities[0].booking_url is None
    assert opportunities[0].attributes == {}


def test_empty_result_is_treated_as_unexpected_page(fixture_html, parser_config):
    with pytest.raises(UnexpectedPageError, match="No elements matched"):
        GenericHtmlParser().parse(
            fixture_html("malformed.html"), BASE_URL, parser_config
        )


def test_duplicate_ids_are_rejected(fixture_html, parser_config):
    with pytest.raises(InvalidOpportunityError, match="Duplicate opportunity IDs"):
        GenericHtmlParser().parse(
            fixture_html("duplicate_ids.html"), BASE_URL, parser_config
        )


def test_unmapped_availability_is_rejected(fixture_html, parser_config):
    html = fixture_html("reservations.html").replace(
        'data-status="available"', 'data-status="maybe"', 1
    )

    with pytest.raises(InvalidOpportunityError, match="unmapped availability"):
        GenericHtmlParser().parse(html, BASE_URL, parser_config)


def test_missing_required_field_is_rejected(fixture_html, parser_config):
    html = fixture_html("reservations.html").replace(
        'class="title"', 'class="removed-title"', 1
    )

    with pytest.raises(InvalidOpportunityError, match="Required title is missing"):
        GenericHtmlParser().parse(html, BASE_URL, parser_config)


def test_invalid_datetime_is_rejected(fixture_html, parser_config):
    html = fixture_html("reservations.html").replace(
        "2026-08-22T09:00:00", "not-a-date", 1
    )

    with pytest.raises(InvalidOpportunityError, match="invalid starts_at"):
        GenericHtmlParser().parse(html, BASE_URL, parser_config)


def test_invalid_container_selector_has_configuration_error(
    fixture_html, parser_config
):
    invalid_config = ParserConfig(
        opportunity_selector="[",
        id=parser_config.id,
        title=parser_config.title,
        availability=parser_config.availability,
        availability_mapping=parser_config.availability_mapping,
    )

    with pytest.raises(ParserConfigurationError, match="Invalid opportunity"):
        GenericHtmlParser().parse(
            fixture_html("reservations.html"), BASE_URL, invalid_config
        )


def test_invalid_nested_selector_has_configuration_error(fixture_html, parser_config):
    invalid_config = ParserConfig(
        opportunity_selector=parser_config.opportunity_selector,
        id=parser_config.id,
        title=ValueSource(selector="["),
        availability=parser_config.availability,
        availability_mapping=parser_config.availability_mapping,
    )

    with pytest.raises(ParserConfigurationError, match="selector for title"):
        GenericHtmlParser().parse(
            fixture_html("reservations.html"), BASE_URL, invalid_config
        )


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"opportunity_selector": " "}, "opportunity selector"),
        ({"availability_mapping": {}}, "availability mapping"),
    ],
)
def test_parser_configuration_is_validated(parser_config, kwargs, message):
    values = {
        "opportunity_selector": parser_config.opportunity_selector,
        "id": parser_config.id,
        "title": parser_config.title,
        "availability": parser_config.availability,
        "availability_mapping": parser_config.availability_mapping,
    }
    values.update(kwargs)

    with pytest.raises(ParserConfigurationError, match=message):
        ParserConfig(**values)
