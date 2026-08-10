"""Declarative parser and driver for simple HTML availability pages."""

from web_checker.drivers.generic_html.driver import (
    GenericHtmlDriver,
    GenericHtmlDriverConfig,
)
from web_checker.drivers.generic_html.parser import (
    GenericHtmlParser,
    ParserConfig,
    ValueSource,
)

__all__ = [
    "GenericHtmlDriver",
    "GenericHtmlDriverConfig",
    "GenericHtmlParser",
    "ParserConfig",
    "ValueSource",
]
