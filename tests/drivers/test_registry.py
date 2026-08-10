from datetime import UTC, datetime

import pytest

from web_checker.core.models import CheckResult
from web_checker.drivers.errors import DriverRegistrationError, UnknownDriverError
from web_checker.drivers.registry import DriverRegistry


class FakeDriver:
    name = "fake"

    def validate_config(self, config):
        return None

    async def check(self, config):
        return CheckResult(datetime.now(UTC), ())


def test_registry_resolves_registered_driver():
    driver = FakeDriver()
    registry = DriverRegistry([driver])

    assert registry.get("fake") is driver
    assert registry.names() == ("fake",)


def test_registry_rejects_duplicate_name():
    registry = DriverRegistry([FakeDriver()])

    with pytest.raises(DriverRegistrationError, match="already registered"):
        registry.register(FakeDriver())


def test_registry_rejects_object_that_does_not_implement_contract():
    with pytest.raises(DriverRegistrationError, match="contract"):
        DriverRegistry([object()])


def test_registry_reports_unknown_driver_and_available_names():
    registry = DriverRegistry([FakeDriver()])

    with pytest.raises(UnknownDriverError, match="registered drivers: fake"):
        registry.get("missing")
