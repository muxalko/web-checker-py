"""Driver and registry errors exposed to application services."""


class DriverError(Exception):
    """Base class for availability-driver failures."""


class DriverRegistrationError(DriverError):
    """Raised when a driver cannot be registered safely."""


class UnknownDriverError(DriverError):
    """Raised when configuration names a driver that is not registered."""
