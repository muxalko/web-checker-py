"""Contract implemented by read-only availability drivers."""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from web_checker.core.models import CheckResult

DriverConfig = Mapping[str, Any]


@runtime_checkable
class AvailabilityDriver(Protocol):
    """Read-only provider integration used to observe opportunities."""

    @property
    def name(self) -> str:
        """Return the stable configuration name of this driver."""
        ...

    def validate_config(self, config: DriverConfig) -> None:
        """Raise a useful exception when provider configuration is invalid."""
        ...

    async def check(self, config: DriverConfig) -> CheckResult:
        """Perform one complete read-only provider observation."""
        ...
