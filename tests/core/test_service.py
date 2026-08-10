import asyncio
from datetime import UTC, datetime

import pytest

from web_checker.config import JobDefinition
from web_checker.core.models import Availability, CheckResult, Opportunity
from web_checker.core.service import CheckService
from web_checker.drivers.generic_html.errors import DriverRequestError
from web_checker.drivers.registry import DriverRegistry
from web_checker.storage import RecordOutcome


class FakeDriver:
    name = "fake"

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error

    def validate_config(self, config):
        return None

    async def check(self, config):
        if self.error is not None:
            raise self.error
        return self.result


class CapturingStore:
    def __init__(self):
        self.calls = []

    def record_success(self, job_id, result, notification_plan):
        self.calls.append((job_id, result, notification_plan))
        return RecordOutcome(baseline_created=True, transitions=())


def snapshot():
    return CheckResult(
        checked_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        opportunities=(
            Opportunity(id="pass", title="Pass", availability=Availability.AVAILABLE),
        ),
    )


def job():
    return JobDefinition(id="job", driver="fake", config={})


def test_successful_driver_result_is_recorded():
    result = snapshot()
    store = CapturingStore()
    service = CheckService(DriverRegistry([FakeDriver(result=result)]), store)

    execution = asyncio.run(service.check(job()))

    assert store.calls == [("job", result, job().notifications)]
    assert execution.result is result
    assert execution.baseline_created is True


def test_failed_driver_result_is_never_recorded():
    store = CapturingStore()
    service = CheckService(
        DriverRegistry([FakeDriver(error=DriverRequestError("failed"))]), store
    )

    with pytest.raises(DriverRequestError):
        asyncio.run(service.check(job()))

    assert store.calls == []
