from copy import deepcopy

import pytest

from web_checker.drivers.bcparks_dayuse.errors import BCParksParseError
from web_checker.drivers.bcparks_dayuse.parser import (
    parse_facilities,
    parse_parks,
    parse_provider_config,
    parse_reservations,
)


def test_parses_captured_provider_contract(fixture_json):
    config = parse_provider_config(fixture_json("config.json"))
    parks = parse_parks(fixture_json("parks.json"))
    facilities = parse_facilities(
        fixture_json("facilities-golden-ears.json"), park_id="0008"
    )
    reservations = parse_reservations(fixture_json("reservations-south-beach.json"))

    assert config.advance_booking_limit == 3
    assert config.parking_pass_limit == 1
    assert [park.id for park in parks] == ["0007", "0008", "0015", "0363"]
    south_beach = facilities[1]
    assert south_beach.booking_times == {"AM": 790, "PM": 420}
    assert south_beach.booking_days[6] is True
    assert south_beach.booking_days[3] is False
    assert reservations[0].capacity == "Full"
    assert reservations[-1].capacity == "High"


@pytest.mark.parametrize("value", [None, {}, []])
def test_empty_or_wrong_park_response_fails(value):
    with pytest.raises(BCParksParseError, match="park response"):
        parse_parks(value)


def test_unknown_capacity_state_fails(fixture_json):
    response = fixture_json("reservations-south-beach.json")
    response["2026-08-16"]["AM"]["capacity"] = "Plenty"

    with pytest.raises(BCParksParseError, match="unknown capacity"):
        parse_reservations(response)


def test_inconsistent_full_capacity_fails(fixture_json):
    response = fixture_json("reservations-south-beach.json")
    response["2026-08-16"]["AM"]["max"] = 1

    with pytest.raises(BCParksParseError, match="inconsistent"):
        parse_reservations(response)


def test_facility_from_another_park_fails(fixture_json):
    response = deepcopy(fixture_json("facilities-golden-ears.json"))
    response[0]["pk"] = "facility::0007"

    with pytest.raises(BCParksParseError, match="unexpected park"):
        parse_facilities(response, park_id="0008")


def test_missing_booking_day_fails(fixture_json):
    response = deepcopy(fixture_json("facilities-golden-ears.json"))
    del response[0]["bookingDays"]["7"]

    with pytest.raises(BCParksParseError, match="days 1 through 7"):
        parse_facilities(response, park_id="0008")
