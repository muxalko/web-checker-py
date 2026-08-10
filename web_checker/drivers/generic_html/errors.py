"""Errors produced while fetching and interpreting generic HTML pages."""

from web_checker.drivers.errors import DriverError


class HtmlParserError(Exception):
    """Base class for generic HTML parser failures."""


class ParserConfigurationError(HtmlParserError):
    """Raised when extraction rules are internally invalid."""


class UnexpectedPageError(HtmlParserError):
    """Raised when a page does not have the expected provider structure."""


class InvalidOpportunityError(HtmlParserError):
    """Raised when an opportunity cannot be normalized safely."""


class GenericHtmlDriverError(DriverError):
    """Base class for failures in the generic HTML HTTP driver."""


class DriverConfigurationError(GenericHtmlDriverError):
    """Raised when generic HTML driver configuration is invalid."""


class DriverRequestError(GenericHtmlDriverError):
    """Raised when an HTTP request cannot be completed."""


class DriverTimeoutError(DriverRequestError):
    """Raised when an HTTP request exceeds its configured timeout."""


class DriverResponseError(GenericHtmlDriverError):
    """Raised when a provider returns an unacceptable response."""


class ResponseTooLargeError(DriverResponseError):
    """Raised when a response exceeds the configured byte limit."""


class DriverParseError(GenericHtmlDriverError):
    """Raised when a successful HTTP response cannot be parsed safely."""
