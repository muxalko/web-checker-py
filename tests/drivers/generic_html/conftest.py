import pytest

from web_checker.core.models import Availability
from web_checker.drivers.generic_html.parser import ParserConfig, ValueSource


@pytest.fixture
def parser_config():
    return ParserConfig(
        opportunity_selector="[data-opportunity-id]",
        id=ValueSource(attribute="data-opportunity-id"),
        title=ValueSource(selector=".title"),
        availability=ValueSource(selector=".status", attribute="data-status"),
        availability_mapping={
            "available": Availability.AVAILABLE,
            "sold-out": Availability.UNAVAILABLE,
            "unknown": Availability.UNKNOWN,
        },
        starts_at=ValueSource(selector="time", attribute="datetime"),
        booking_url=ValueSource(selector="a.book", attribute="href", required=False),
        attributes={
            "capacity": ValueSource(
                selector=".status", attribute="data-capacity", required=False
            )
        },
    )
