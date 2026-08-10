"""Safe loading and validation of declarative check jobs."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from web_checker.core.transitions import TransitionType
from web_checker.notifications.models import NotificationPlan

JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class _Yaml12SafeLoader(yaml.SafeLoader):
    """Safe loader whose booleans follow YAML 1.2 true/false semantics."""


_Yaml12SafeLoader.yaml_implicit_resolvers = {
    key: list(resolvers)
    for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for resolver_key, resolvers in _Yaml12SafeLoader.yaml_implicit_resolvers.items():
    _Yaml12SafeLoader.yaml_implicit_resolvers[resolver_key] = [
        (tag, pattern) for tag, pattern in resolvers if tag != "tag:yaml.org,2002:bool"
    ]
_Yaml12SafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


class ConfigurationError(Exception):
    """Raised when application configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry policy applied by the scheduler after failed checks."""

    max_attempts: int = 3
    initial_delay_seconds: float = 5.0
    max_delay_seconds: float = 60.0
    jitter_fraction: float = 0.2

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or not 1 <= self.max_attempts <= 10
        ):
            raise ConfigurationError("retry max_attempts must be between 1 and 10")
        _validate_finite_number(
            self.initial_delay_seconds,
            "retry initial_delay_seconds",
            minimum=0,
        )
        _validate_finite_number(
            self.max_delay_seconds,
            "retry max_delay_seconds",
            minimum=0,
        )
        _validate_finite_number(
            self.jitter_fraction,
            "retry jitter_fraction",
            minimum=0,
            maximum=1,
        )
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ConfigurationError(
                "retry max_delay_seconds must be greater than or equal to "
                "initial_delay_seconds"
            )
        if self.max_attempts > 1 and self.initial_delay_seconds == 0:
            raise ConfigurationError(
                "retry initial_delay_seconds must be greater than 0 when "
                "max_attempts is greater than 1"
            )

    def delay_after_failure(self, failed_attempt: int, random_unit: float) -> float:
        """Return capped exponential delay with symmetric multiplicative jitter."""
        base_delay = min(
            self.initial_delay_seconds * (2 ** (failed_attempt - 1)),
            self.max_delay_seconds,
        )
        jitter_multiplier = 1 + self.jitter_fraction * (2 * random_unit - 1)
        return min(
            self.max_delay_seconds,
            max(0.0, base_delay * jitter_multiplier),
        )


@dataclass(frozen=True, slots=True)
class ScheduleDefinition:
    """Provider-independent interval schedule for one job."""

    interval_seconds: float
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    def __post_init__(self) -> None:
        _validate_finite_number(
            self.interval_seconds,
            "schedule interval_seconds",
            minimum=0,
            minimum_inclusive=False,
        )


@dataclass(frozen=True, slots=True)
class JobDefinition:
    """Common provider-independent configuration for one check job."""

    id: str
    driver: str
    config: Mapping[str, Any]
    enabled: bool = True
    notifications: NotificationPlan = field(default_factory=NotificationPlan)
    schedule: ScheduleDefinition | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not JOB_ID_PATTERN.fullmatch(self.id):
            raise ConfigurationError(
                "job id must start with an alphanumeric character and contain only "
                "letters, numbers, dots, underscores, or hyphens"
            )
        if not isinstance(self.driver, str) or not self.driver.strip():
            raise ConfigurationError(f"job {self.id!r} driver must not be blank")
        if not isinstance(self.enabled, bool):
            raise ConfigurationError(f"job {self.id!r} enabled must be a boolean")
        if not isinstance(self.config, Mapping):
            raise ConfigurationError(f"job {self.id!r} config must be a mapping")

        object.__setattr__(self, "driver", self.driver.strip())
        object.__setattr__(self, "config", _freeze_mapping(self.config))


@dataclass(frozen=True, slots=True)
class ApplicationConfig:
    """Validated collection of uniquely named check jobs."""

    jobs: tuple[JobDefinition, ...]

    def __post_init__(self) -> None:
        ids = [job.id for job in self.jobs]
        duplicates = sorted({job_id for job_id in ids if ids.count(job_id) > 1})
        if duplicates:
            raise ConfigurationError(f"duplicate job ids: {', '.join(duplicates)}")

    def get_job(self, job_id: str) -> JobDefinition:
        for job in self.jobs:
            if job.id == job_id:
                return job
        available = ", ".join(job.id for job in self.jobs) or "none"
        raise ConfigurationError(
            f"unknown job {job_id!r}; configured jobs: {available}"
        )


def load_config(path: str | Path) -> ApplicationConfig:
    """Load YAML safely and validate all provider-independent job fields."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ConfigurationError(f"could not read {config_path}: {error}") from error
    try:
        document = yaml.load(text, Loader=_Yaml12SafeLoader)
    except yaml.YAMLError as error:
        raise ConfigurationError(f"invalid YAML in {config_path}: {error}") from error

    if not isinstance(document, Mapping):
        raise ConfigurationError("configuration root must be a mapping")
    unexpected_root = set(document) - {"jobs"}
    if unexpected_root:
        raise ConfigurationError(
            "unsupported root fields: "
            + ", ".join(sorted(str(item) for item in unexpected_root))
        )
    raw_jobs = document.get("jobs")
    if not isinstance(raw_jobs, list) or not raw_jobs:
        raise ConfigurationError("jobs must be a non-empty list")

    jobs = tuple(_parse_job(raw_job, index) for index, raw_job in enumerate(raw_jobs))
    return ApplicationConfig(jobs=jobs)


def _parse_job(value: Any, index: int) -> JobDefinition:
    label = f"jobs[{index}]"
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{label} must be a mapping")
    allowed = {"id", "driver", "config", "enabled", "notify", "schedule"}
    unexpected = set(value) - allowed
    if unexpected:
        raise ConfigurationError(
            f"unsupported {label} fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    missing = {"id", "driver", "config"} - set(value)
    if missing:
        raise ConfigurationError(
            f"missing {label} fields: {', '.join(sorted(missing))}"
        )
    return JobDefinition(
        id=value["id"],
        driver=value["driver"],
        config=value["config"],
        enabled=value.get("enabled", True),
        notifications=_parse_notifications(value.get("notify"), label),
        schedule=_parse_schedule(value.get("schedule"), label),
    )


def _parse_schedule(value: Any, job_label: str) -> ScheduleDefinition | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{job_label}.schedule must be a mapping")
    unexpected = set(value) - {"interval_seconds", "retry"}
    if unexpected:
        raise ConfigurationError(
            f"unsupported {job_label}.schedule fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    if "interval_seconds" not in value:
        raise ConfigurationError(
            f"missing {job_label}.schedule fields: interval_seconds"
        )
    retry_value = value.get("retry")
    if retry_value is None:
        retry = RetryPolicy()
    else:
        if not isinstance(retry_value, Mapping):
            raise ConfigurationError(f"{job_label}.schedule.retry must be a mapping")
        retry_fields = {
            "max_attempts",
            "initial_delay_seconds",
            "max_delay_seconds",
            "jitter_fraction",
        }
        unexpected_retry = set(retry_value) - retry_fields
        if unexpected_retry:
            raise ConfigurationError(
                f"unsupported {job_label}.schedule.retry fields: "
                + ", ".join(sorted(str(item) for item in unexpected_retry))
            )
        try:
            retry = RetryPolicy(**retry_value)
        except TypeError as error:
            raise ConfigurationError(
                f"invalid {job_label}.schedule.retry: {error}"
            ) from error
    try:
        return ScheduleDefinition(
            interval_seconds=value["interval_seconds"],
            retry=retry,
        )
    except ConfigurationError as error:
        raise ConfigurationError(f"invalid {job_label}.schedule: {error}") from error


def _parse_notifications(value: Any, job_label: str) -> NotificationPlan:
    if value is None:
        return NotificationPlan()
    if not isinstance(value, Mapping):
        raise ConfigurationError(f"{job_label}.notify must be a mapping")
    unexpected = set(value) - {"channels", "on"}
    if unexpected:
        raise ConfigurationError(
            f"unsupported {job_label}.notify fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    missing = {"channels", "on"} - set(value)
    if missing:
        raise ConfigurationError(
            f"missing {job_label}.notify fields: {', '.join(sorted(missing))}"
        )
    channels = value["channels"]
    transition_values = value["on"]
    if not isinstance(channels, list) or not channels:
        raise ConfigurationError(
            f"{job_label}.notify.channels must be a non-empty list"
        )
    if not all(isinstance(channel, str) for channel in channels):
        raise ConfigurationError(f"{job_label}.notify.channels must contain strings")
    if not isinstance(transition_values, list) or not transition_values:
        raise ConfigurationError(f"{job_label}.notify.on must be a non-empty list")
    transitions: set[TransitionType] = set()
    for transition_value in transition_values:
        if not isinstance(transition_value, str):
            raise ConfigurationError(f"{job_label}.notify.on must contain strings")
        try:
            transitions.add(TransitionType(transition_value))
        except ValueError as error:
            raise ConfigurationError(
                f"{job_label}.notify.on contains unknown transition "
                f"{transition_value!r}"
            ) from error
    try:
        return NotificationPlan(
            channels=tuple(channels),
            on=frozenset(transitions),
        )
    except ValueError as error:
        raise ConfigurationError(f"invalid {job_label}.notify: {error}") from error


def _freeze_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType({key: freeze(child) for key, child in item.items()})
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    return freeze(value)


def _validate_finite_number(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float | None = None,
    minimum_inclusive: bool = True,
) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
    ):
        raise ConfigurationError(f"{label} must be a finite number")
    minimum_valid = value >= minimum if minimum_inclusive else value > minimum
    if not minimum_valid:
        comparison = "greater than or equal to" if minimum_inclusive else "greater than"
        raise ConfigurationError(f"{label} must be {comparison} {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{label} must be less than or equal to {maximum}")
