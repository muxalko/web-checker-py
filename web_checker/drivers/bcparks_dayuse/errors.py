"""Errors raised by the BC Parks day-use driver."""

from web_checker.drivers.errors import DriverError


class BCParksDriverError(DriverError):
    """Base class for BC Parks driver failures."""


class BCParksConfigurationError(BCParksDriverError):
    """Raised when BC Parks driver configuration is invalid."""


class BCParksRequestError(BCParksDriverError):
    """Raised when a BC Parks request cannot be completed."""


class BCParksTimeoutError(BCParksRequestError):
    """Raised when a BC Parks request exceeds its timeout."""


class BCParksResponseError(BCParksDriverError):
    """Raised when BC Parks returns an unacceptable HTTP response."""


class BCParksResponseTooLargeError(BCParksResponseError):
    """Raised when a BC Parks response exceeds the configured size limit."""


class BCParksParseError(BCParksDriverError):
    """Raised when a BC Parks response is missing required structure."""


class BCParksProviderClosedError(BCParksDriverError):
    """Raised when the selected park or facility is not open."""
