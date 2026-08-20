"""Strict parser for WelcomeBC High Economic Impact invitation draws."""

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from web_checker.core.models import Availability, Opportunity
from web_checker.drivers.welcomebc_high_impact.errors import (
    WelcomeBCInvalidDrawError,
    WelcomeBCUnexpectedPageError,
)

HIGH_IMPACT_TYPE = "Innovate: High Economic Impact"
EXPECTED_HEADERS = (
    "date",
    "ita type",
    "selection factors",
    "minimum score",
    "number of invitations",
)
VANCOUVER = ZoneInfo("America/Vancouver")


@dataclass(frozen=True, slots=True)
class SelectionRoute:
    """One threshold route within a High Economic Impact draw."""

    selection_factors: str
    minimum_score: str
    invitations: str
    invitation_count: int | None


@dataclass(frozen=True, slots=True)
class HighImpactDraw:
    """Provider-specific representation grouped by draw date."""

    draw_date: date
    routes: tuple[SelectionRoute, ...]

    @property
    def total_invitations(self) -> int | None:
        if any(route.invitation_count is None for route in self.routes):
            return None
        return sum(route.invitation_count or 0 for route in self.routes)


class WelcomeBCHighImpactParser:
    """Converts the public WelcomeBC page into one opportunity per draw."""

    def parse(self, html: str, source_url: str) -> tuple[Opportunity, ...]:
        soup = BeautifulSoup(html, "html.parser")
        skills_section = soup.select_one("#Skills_Immigration_invitations")
        if skills_section is None:
            raise WelcomeBCUnexpectedPageError(
                "Skills Immigration invitations section is missing"
            )

        content = skills_section.select_one(".accordion-body")
        if content is None:
            raise WelcomeBCUnexpectedPageError(
                "Skills Immigration invitations content is missing"
            )

        table = self._find_draw_table(content)
        draws = self._parse_table_draws(table)
        for draw in self._parse_narrative_draws(content):
            # During page edits a draw may temporarily exist in both formats. The
            # structured table is authoritative and already carries the same ID.
            draws.setdefault(draw.draw_date, draw)

        if not draws:
            raise WelcomeBCUnexpectedPageError(
                "No High Economic Impact invitation draws were found"
            )

        return tuple(
            self._to_opportunity(draws[draw_date], source_url)
            for draw_date in sorted(draws, reverse=True)
        )

    @staticmethod
    def _find_draw_table(content: Tag) -> Tag:
        for table in content.find_all("table"):
            first_row = table.find("tr")
            if first_row is None:
                continue
            headers = tuple(
                _text(cell).casefold()
                for cell in first_row.find_all(["th", "td"], recursive=False)
            )
            if headers == EXPECTED_HEADERS:
                return table
        raise WelcomeBCUnexpectedPageError(
            "Skills Immigration invitation table headers changed or are missing"
        )

    def _parse_table_draws(self, table: Tag) -> dict[date, HighImpactDraw]:
        rows = table.find_all("tr")
        grouped: dict[date, list[SelectionRoute]] = {}
        route_keys: dict[date, set[tuple[str, str]]] = {}
        current_date: date | None = None
        remaining_date_rows = 0

        for row in rows[1:]:
            cells = row.find_all(["th", "td"], recursive=False)
            values = tuple(_text(cell) for cell in cells)
            if len(values) == 5:
                raw_date, ita_type, factors, score, invitations = values
                try:
                    current_date = _parse_date(raw_date)
                    remaining_date_rows = _rowspan(cells[0]) - 1
                except WelcomeBCInvalidDrawError:
                    current_date = None
                    remaining_date_rows = 0
                    if _is_high_impact(ita_type):
                        raise
                    continue
            elif len(values) == 4:
                ita_type, factors, score, invitations = values
                if remaining_date_rows <= 0:
                    current_date = None
                else:
                    remaining_date_rows -= 1
            else:
                if remaining_date_rows > 0:
                    remaining_date_rows -= 1
                if any("high economic impact" in value.casefold() for value in values):
                    raise WelcomeBCInvalidDrawError(
                        "High Economic Impact table row has an unexpected column count"
                    )
                continue

            if not _is_high_impact(ita_type):
                continue
            if current_date is None:
                raise WelcomeBCInvalidDrawError(
                    "High Economic Impact table row has no valid draw date"
                )

            route = _table_route(factors, score, invitations)
            key = (route.selection_factors.casefold(), route.minimum_score.casefold())
            keys = route_keys.setdefault(current_date, set())
            if key in keys:
                raise WelcomeBCInvalidDrawError(
                    f"Draw {current_date.isoformat()} has duplicate selection routes"
                )
            keys.add(key)
            grouped.setdefault(current_date, []).append(route)

        return {
            draw_date: HighImpactDraw(draw_date, tuple(routes))
            for draw_date, routes in grouped.items()
        }

    def _parse_narrative_draws(self, content: Tag) -> tuple[HighImpactDraw, ...]:
        draws: dict[date, HighImpactDraw] = {}
        for heading in content.find_all("h3"):
            try:
                draw_date = _parse_date(_text(heading))
            except WelcomeBCInvalidDrawError:
                continue

            section = _section_after(heading)
            section_text = " ".join(_text(item) for item in section)
            if "high economic impact" not in section_text.casefold():
                continue

            total_match = re.search(
                r"issued invitations to apply to\s+([\d,]+)\s+candidates"
                r"\s+who will create high economic impact",
                section_text,
                flags=re.IGNORECASE,
            )
            if total_match is None:
                raise WelcomeBCInvalidDrawError(
                    f"Narrative draw {draw_date.isoformat()} has no valid total count"
                )
            expected_total = _integer(total_match.group(1), "narrative total")

            list_items = [
                item for element in section for item in element.find_all("li")
            ]
            if not list_items:
                raise WelcomeBCInvalidDrawError(
                    f"Narrative draw {draw_date.isoformat()} has no selection routes"
                )
            routes = tuple(_narrative_route(_text(item)) for item in list_items)
            actual_total = sum(route.invitation_count or 0 for route in routes)
            if actual_total != expected_total:
                raise WelcomeBCInvalidDrawError(
                    f"Narrative draw {draw_date.isoformat()} invitation counts "
                    f"sum to {actual_total}, expected {expected_total}"
                )

            draw = HighImpactDraw(draw_date, routes)
            existing = draws.get(draw_date)
            if existing is not None and existing != draw:
                raise WelcomeBCInvalidDrawError(
                    f"Narrative draw {draw_date.isoformat()} is inconsistent"
                )
            draws[draw_date] = draw
        return tuple(draws.values())

    @staticmethod
    def _to_opportunity(draw: HighImpactDraw, source_url: str) -> Opportunity:
        formatted_date = (
            f"{draw.draw_date:%B} {draw.draw_date.day}, {draw.draw_date:%Y}"
        )
        total = draw.total_invitations
        total_suffix = f" ({total} invitations)" if total is not None else ""
        attributes: dict[str, object] = {
            "draw_date": draw.draw_date.isoformat(),
            "ita_type": HIGH_IMPACT_TYPE,
            "selection_routes": tuple(
                {
                    "selection_factors": route.selection_factors,
                    "minimum_score": route.minimum_score,
                    "invitations": route.invitations,
                }
                for route in draw.routes
            ),
        }
        if total is not None:
            attributes["total_invitations"] = total

        return Opportunity(
            id=f"high-economic-impact-{draw.draw_date.isoformat()}",
            title=f"BC PNP High Economic Impact draw on {formatted_date}{total_suffix}",
            availability=Availability.AVAILABLE,
            starts_at=datetime.combine(draw.draw_date, time.min, tzinfo=VANCOUVER),
            booking_url=source_url,
            attributes=attributes,
        )


def _text(element: Tag) -> str:
    return " ".join(element.get_text(" ", strip=True).split())


def _parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%B %d, %Y").date()
    except ValueError as error:
        raise WelcomeBCInvalidDrawError(f"Invalid draw date {value!r}") from error


def _rowspan(cell: Tag) -> int:
    raw_value = cell.get("rowspan", "1")
    try:
        value = int(str(raw_value))
    except ValueError as error:
        raise WelcomeBCInvalidDrawError(
            f"Invalid date rowspan {raw_value!r}"
        ) from error
    if value <= 0:
        raise WelcomeBCInvalidDrawError(f"Invalid date rowspan {raw_value!r}")
    return value


def _is_high_impact(value: str) -> bool:
    return value.casefold() == HIGH_IMPACT_TYPE.casefold()


def _table_route(factors: str, score: str, invitations: str) -> SelectionRoute:
    if not factors:
        raise WelcomeBCInvalidDrawError("Selection factors must not be blank")
    if not re.fullmatch(r"(?:N/A|[\d,]+)", score, flags=re.IGNORECASE):
        raise WelcomeBCInvalidDrawError(f"Invalid minimum score {score!r}")
    invitation_count = _invitation_count(invitations)
    return SelectionRoute(factors, score, invitations, invitation_count)


def _narrative_route(value: str) -> SelectionRoute:
    count_match = re.search(r"\(([\d,]+)\s+candidates?\)", value, re.IGNORECASE)
    if count_match is None:
        raise WelcomeBCInvalidDrawError(
            f"Narrative selection route has no invitation count: {value!r}"
        )
    count = _integer(count_match.group(1), "narrative invitation count")
    score_match = re.search(r"minimum score of\s+([\d,]+)", value, re.IGNORECASE)
    score = score_match.group(1) if score_match is not None else "N/A"
    factors = re.sub(
        r"\s*\([\d,]+\s+candidates?\)\s*(?:,\s*or)?\s*$",
        "",
        value,
        flags=re.IGNORECASE,
    ).rstrip(" .,;")
    if not factors:
        raise WelcomeBCInvalidDrawError("Narrative selection factors must not be blank")
    return SelectionRoute(factors, score, str(count), count)


def _invitation_count(value: str) -> int | None:
    if re.fullmatch(r"[\d,]+", value):
        return _integer(value, "invitation count")
    if re.fullmatch(r"<\s*\d+", value):
        return None
    raise WelcomeBCInvalidDrawError(f"Invalid invitation count {value!r}")


def _integer(value: str, field: str) -> int:
    try:
        result = int(value.replace(",", ""))
    except ValueError as error:
        raise WelcomeBCInvalidDrawError(f"Invalid {field} {value!r}") from error
    if result <= 0:
        raise WelcomeBCInvalidDrawError(f"Invalid {field} {value!r}")
    return result


def _section_after(heading: Tag) -> tuple[Tag, ...]:
    section: list[Tag] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "h3":
            break
        if isinstance(sibling, Tag):
            section.append(sibling)
    return tuple(section)
