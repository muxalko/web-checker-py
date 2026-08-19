"""Errors produced while fetching and interpreting WelcomeBC ITA pages."""

from web_checker.drivers.errors import DriverError


class WelcomeBCParserError(Exception):
    """Base class for WelcomeBC parser failures."""


class WelcomeBCUnexpectedPageError(WelcomeBCParserError):
    """Raised when the expected Skills Immigration content is absent."""


class WelcomeBCInvalidDrawError(WelcomeBCParserError):
    """Raised when a High Economic Impact draw cannot be normalized safely."""


class WelcomeBCHighImpactDriverError(DriverError):
    """Base class for WelcomeBC High Economic Impact driver failures."""


class WelcomeBCConfigurationError(WelcomeBCHighImpactDriverError):
    """Raised when driver configuration is invalid."""


class WelcomeBCRequestError(WelcomeBCHighImpactDriverError):
    """Raised when the provider request cannot be completed."""


class WelcomeBCTimeoutError(WelcomeBCRequestError):
    """Raised when the provider request exceeds its configured timeout."""


class WelcomeBCResponseError(WelcomeBCHighImpactDriverError):
    """Raised when the provider returns an unacceptable response."""


class WelcomeBCResponseTooLargeError(WelcomeBCResponseError):
    """Raised when the response exceeds its configured byte limit."""


class WelcomeBCParseError(WelcomeBCHighImpactDriverError):
    """Raised when a successful response cannot be parsed safely."""
