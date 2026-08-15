"""Explicit registry for interchangeable provider drivers."""

from collections.abc import Iterable

from web_checker.drivers.base import AvailabilityDriver
from web_checker.drivers.errors import DriverRegistrationError, UnknownDriverError


class DriverRegistry:
    """Stores driver instances by stable, unique name."""

    def __init__(self, drivers: Iterable[AvailabilityDriver] = ()) -> None:
        self._drivers: dict[str, AvailabilityDriver] = {}
        for driver in drivers:
            self.register(driver)

    def register(self, driver: AvailabilityDriver) -> None:
        if not isinstance(driver, AvailabilityDriver):
            raise DriverRegistrationError(
                "Driver does not implement the availability-driver contract"
            )
        name = driver.name.strip()
        if not name:
            raise DriverRegistrationError("Driver name must not be blank")
        if name in self._drivers:
            raise DriverRegistrationError(
                f"A driver named {name!r} is already registered"
            )
        self._drivers[name] = driver

    def get(self, name: str) -> AvailabilityDriver:
        try:
            return self._drivers[name]
        except KeyError as error:
            available = ", ".join(self.names()) or "none"
            raise UnknownDriverError(
                f"Unknown driver {name!r}; registered drivers: {available}"
            ) from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._drivers))


def create_default_registry() -> DriverRegistry:
    """Build the registry of drivers shipped with this application."""
    from web_checker.drivers.bcparks_dayuse.driver import BCParksDayUseDriver
    from web_checker.drivers.generic_html.driver import GenericHtmlDriver

    return DriverRegistry([BCParksDayUseDriver(), GenericHtmlDriver()])
