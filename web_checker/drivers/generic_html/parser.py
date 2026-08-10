"""Pure, declaratively configured HTML-to-opportunity parser."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from soupsieve.util import SelectorSyntaxError

from web_checker.core.models import Availability, Opportunity
from web_checker.drivers.generic_html.errors import (
    InvalidOpportunityError,
    ParserConfigurationError,
    UnexpectedPageError,
)


@dataclass(frozen=True, slots=True)
class ValueSource:
    """Describes text or attribute extraction relative to an opportunity."""

    selector: str | None = None
    attribute: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if self.selector is not None and not self.selector.strip():
            raise ParserConfigurationError("A selector must not be blank")
        if self.attribute is not None and not self.attribute.strip():
            raise ParserConfigurationError("An attribute must not be blank")

    @classmethod
    def from_mapping(cls, value: Any, field_name: str) -> "ValueSource":
        if not isinstance(value, Mapping):
            raise ParserConfigurationError(f"{field_name} must be a mapping")
        allowed = {"selector", "attribute", "required"}
        unexpected = set(value) - allowed
        if unexpected:
            raise ParserConfigurationError(
                f"Unsupported {field_name} fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        if "required" in value and not isinstance(value["required"], bool):
            raise ParserConfigurationError(f"{field_name}.required must be a boolean")
        for key in ("selector", "attribute"):
            if (
                key in value
                and value[key] is not None
                and not isinstance(value[key], str)
            ):
                raise ParserConfigurationError(f"{field_name}.{key} must be a string")
        return cls(
            selector=value.get("selector"),
            attribute=value.get("attribute"),
            required=value.get("required", True),
        )


@dataclass(frozen=True, slots=True)
class ParserConfig:
    """Typed extraction rules for repeated opportunities on one HTML page."""

    opportunity_selector: str
    id: ValueSource
    title: ValueSource
    availability: ValueSource
    availability_mapping: Mapping[str, Availability]
    starts_at: ValueSource | None = None
    booking_url: ValueSource | None = None
    attributes: Mapping[str, ValueSource] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.opportunity_selector.strip():
            raise ParserConfigurationError("The opportunity selector must not be blank")
        if not self.availability_mapping:
            raise ParserConfigurationError(
                "At least one availability mapping is required"
            )
        if any(not key.strip() for key in self.availability_mapping):
            raise ParserConfigurationError(
                "Availability mapping keys must not be blank"
            )
        if any(
            not isinstance(value, Availability)
            for value in self.availability_mapping.values()
        ):
            raise ParserConfigurationError(
                "Availability mappings must use normalized Availability values"
            )
        if any(not name.strip() for name in self.attributes):
            raise ParserConfigurationError("Attribute names must not be blank")

        object.__setattr__(
            self,
            "availability_mapping",
            MappingProxyType(dict(self.availability_mapping)),
        )
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    @classmethod
    def from_mapping(cls, value: Any) -> "ParserConfig":
        if not isinstance(value, Mapping):
            raise ParserConfigurationError("parser must be a mapping")
        allowed = {
            "opportunity_selector",
            "id",
            "title",
            "availability",
            "availability_mapping",
            "starts_at",
            "booking_url",
            "attributes",
        }
        unexpected = set(value) - allowed
        if unexpected:
            raise ParserConfigurationError(
                "Unsupported parser fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        required = {
            "opportunity_selector",
            "id",
            "title",
            "availability",
            "availability_mapping",
        }
        missing = required - set(value)
        if missing:
            raise ParserConfigurationError(
                f"Missing parser fields: {', '.join(sorted(missing))}"
            )
        if not isinstance(value["opportunity_selector"], str):
            raise ParserConfigurationError("opportunity_selector must be a string")
        raw_mapping = value["availability_mapping"]
        if not isinstance(raw_mapping, Mapping):
            raise ParserConfigurationError("availability_mapping must be a mapping")
        availability_mapping: dict[str, Availability] = {}
        for provider_value, normalized_value in raw_mapping.items():
            if not isinstance(provider_value, str) or not isinstance(
                normalized_value, str
            ):
                raise ParserConfigurationError(
                    "availability_mapping keys and values must be strings"
                )
            try:
                availability_mapping[provider_value] = Availability(normalized_value)
            except ValueError as error:
                raise ParserConfigurationError(
                    f"Unknown normalized availability {normalized_value!r}"
                ) from error

        raw_attributes = value.get("attributes", {})
        if not isinstance(raw_attributes, Mapping):
            raise ParserConfigurationError("attributes must be a mapping")
        attributes = {
            name: ValueSource.from_mapping(source, f"attributes.{name}")
            for name, source in raw_attributes.items()
            if isinstance(name, str)
        }
        if len(attributes) != len(raw_attributes):
            raise ParserConfigurationError("attribute names must be strings")

        def optional_source(name: str) -> ValueSource | None:
            source = value.get(name)
            if source is None:
                return None
            return ValueSource.from_mapping(source, name)

        return cls(
            opportunity_selector=value["opportunity_selector"],
            id=ValueSource.from_mapping(value["id"], "id"),
            title=ValueSource.from_mapping(value["title"], "title"),
            availability=ValueSource.from_mapping(
                value["availability"], "availability"
            ),
            availability_mapping=availability_mapping,
            starts_at=optional_source("starts_at"),
            booking_url=optional_source("booking_url"),
            attributes=attributes,
        )


class GenericHtmlParser:
    """Converts HTML into normalized opportunities without performing I/O."""

    def parse(
        self, html: str, base_url: str, config: ParserConfig
    ) -> tuple[Opportunity, ...]:
        soup = BeautifulSoup(html, "html.parser")
        try:
            containers = soup.select(config.opportunity_selector)
        except SelectorSyntaxError as error:
            raise ParserConfigurationError(
                f"Invalid opportunity selector: {config.opportunity_selector!r}"
            ) from error

        if not containers:
            raise UnexpectedPageError(
                f"No elements matched {config.opportunity_selector!r}"
            )

        opportunities = tuple(
            self._parse_opportunity(container, base_url, config)
            for container in containers
        )
        ids = [opportunity.id for opportunity in opportunities]
        duplicates = sorted({item for item in ids if ids.count(item) > 1})
        if duplicates:
            raise InvalidOpportunityError(
                f"Duplicate opportunity IDs: {', '.join(duplicates)}"
            )
        return opportunities

    def _parse_opportunity(
        self, container: Tag, base_url: str, config: ParserConfig
    ) -> Opportunity:
        opportunity_id = self._extract(container, config.id, "id")
        title = self._extract(container, config.title, "title")
        provider_availability = self._extract(
            container, config.availability, "availability"
        )
        try:
            availability = config.availability_mapping[provider_availability]
        except KeyError as error:
            raise InvalidOpportunityError(
                f"Opportunity {opportunity_id!r} has unmapped availability "
                f"{provider_availability!r}"
            ) from error

        starts_at = None
        if config.starts_at is not None:
            raw_starts_at = self._extract(container, config.starts_at, "starts_at")
            if raw_starts_at is not None:
                try:
                    starts_at = datetime.fromisoformat(raw_starts_at)
                except ValueError as error:
                    raise InvalidOpportunityError(
                        f"Opportunity {opportunity_id!r} has invalid starts_at "
                        f"{raw_starts_at!r}"
                    ) from error

        booking_url = None
        if config.booking_url is not None:
            raw_booking_url = self._extract(
                container, config.booking_url, "booking_url"
            )
            if raw_booking_url is not None:
                booking_url = urljoin(base_url, raw_booking_url)

        attributes = {
            name: value
            for name, source in config.attributes.items()
            if (value := self._extract(container, source, f"attribute {name!r}"))
            is not None
        }

        try:
            return Opportunity(
                id=opportunity_id,
                title=title,
                availability=availability,
                starts_at=starts_at,
                booking_url=booking_url,
                attributes=attributes,
            )
        except (TypeError, ValueError) as error:
            raise InvalidOpportunityError(str(error)) from error

    def _extract(
        self, container: Tag, source: ValueSource, field_name: str
    ) -> str | None:
        element = container
        if source.selector is not None:
            try:
                selected = container.select_one(source.selector)
            except SelectorSyntaxError as error:
                raise ParserConfigurationError(
                    f"Invalid selector for {field_name}: {source.selector!r}"
                ) from error
            if selected is None:
                return self._missing(source, field_name)
            element = selected

        if source.attribute is not None:
            raw_value = element.get(source.attribute)
            if raw_value is None:
                return self._missing(source, field_name)
            if isinstance(raw_value, list):
                value = " ".join(raw_value)
            else:
                value = str(raw_value)
        else:
            value = element.get_text(" ", strip=True)

        value = " ".join(value.split())
        if not value:
            return self._missing(source, field_name)
        return value

    @staticmethod
    def _missing(source: ValueSource, field_name: str) -> None:
        if source.required:
            raise InvalidOpportunityError(f"Required {field_name} is missing")
        return None
