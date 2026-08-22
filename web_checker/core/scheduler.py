"""Small interval scheduler around the provider-independent check service."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from typing import Protocol

from web_checker.config import JobDefinition

Sleep = Callable[[float], Awaitable[None]]
EventSink = Callable[[str], None]


class CheckRunner(Protocol):
    """Execution boundary used by the scheduler."""

    async def check(self, job: JobDefinition): ...


class OperationalAlerts(Protocol):
    async def check_failed(
        self, job_id: str, error: str, *, alert_after: int, channels: tuple[str, ...]
    ) -> None: ...

    async def dispatch_pending(self) -> None: ...

    async def delivery_backlog(
        self, *, alert_after: int, channels: tuple[str, ...]
    ) -> None: ...


class Scheduler:
    """Runs interval jobs concurrently while serializing each individual job."""

    def __init__(
        self,
        jobs: Iterable[JobDefinition],
        check_runner: CheckRunner,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        random_unit: Callable[[], float] = random.random,
        event_sink: EventSink | None = None,
        shutdown_timeout_seconds: float = 30.0,
        operational_alerts: OperationalAlerts | None = None,
        failure_alert_after: int = 3,
        operational_channels: tuple[str, ...] = ("console",),
    ) -> None:
        self._jobs = tuple(
            job for job in jobs if job.enabled and job.schedule is not None
        )
        self._check_runner = check_runner
        self._monotonic = monotonic
        self._sleep = sleep
        self._random_unit = random_unit
        self._event_sink = event_sink or (lambda message: None)
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._operational_alerts = operational_alerts
        self._failure_alert_after = failure_alert_after
        self._operational_channels = operational_channels
        startup_time = monotonic()
        self._next_due = {job.id: startup_time for job in self._jobs}
        self._tasks: dict[str, asyncio.Task[None]] = {}

    @property
    def running_job_ids(self) -> frozenset[str]:
        """Return jobs whose current scheduled execution has not completed."""
        return frozenset(
            job_id for job_id, task in self._tasks.items() if not task.done()
        )

    async def tick(self) -> None:
        """Start all currently due jobs without waiting for their completion."""
        self._discard_completed_tasks()
        now = self._monotonic()
        for job in self._jobs:
            due_at = self._next_due[job.id]
            if now < due_at:
                continue
            schedule = job.schedule
            assert schedule is not None
            elapsed_intervals = int((now - due_at) // schedule.interval_seconds) + 1
            self._next_due[job.id] = (
                due_at + elapsed_intervals * schedule.interval_seconds
            )
            running = self._tasks.get(job.id)
            if running is not None and not running.done():
                self._emit(job.id, "overlap_skipped")
                continue
            self._tasks[job.id] = asyncio.create_task(
                self._execute_with_retries(job),
                name=f"check:{job.id}",
            )
        # Give newly created tasks a chance to begin. This also makes tick useful
        # as a deterministic test seam without waiting for complete checks.
        await asyncio.sleep(0)

    async def wait_for_idle(self) -> None:
        """Wait until executions that have already been scheduled finish."""
        tasks = tuple(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks)
        self._discard_completed_tasks()

    async def run(self, stop_event: asyncio.Event) -> None:
        """Run until stopped, then give in-flight executions time to finish."""
        try:
            while not stop_event.is_set():
                await self.tick()
                timeout = self._seconds_until_next_due()
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=timeout)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Wait a bounded time for current checks, then cancel any remaining."""
        tasks = tuple(task for task in self._tasks.values() if not task.done())
        if not tasks:
            self._discard_completed_tasks()
            return
        _, pending = await asyncio.wait(
            tasks,
            timeout=self._shutdown_timeout_seconds,
        )
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for job_id, task in self._tasks.items():
                if task in pending:
                    self._emit(job_id, "shutdown_cancelled")
        self._discard_completed_tasks()

    async def _execute_with_retries(self, job: JobDefinition) -> None:
        schedule = job.schedule
        assert schedule is not None
        retry = schedule.retry
        for attempt in range(1, retry.max_attempts + 1):
            self._emit(job.id, "check_started", attempt=attempt)
            started_at = self._monotonic()
            try:
                execution = await self._check_runner.check(job)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if attempt == retry.max_attempts:
                    formatted_error = f"{type(error).__name__}: {error}"
                    self._emit(
                        job.id,
                        "check_failed",
                        attempt=attempt,
                        error=formatted_error,
                    )
                    if self._operational_alerts is not None:
                        await self._operational_alerts.check_failed(
                            job.id,
                            formatted_error,
                            alert_after=self._failure_alert_after,
                            channels=self._operational_channels,
                        )
                    return
                delay = retry.delay_after_failure(attempt, self._random_unit())
                self._emit(
                    job.id,
                    "retry_scheduled",
                    attempt=attempt,
                    delay_seconds=f"{delay:.3f}",
                    error=f"{type(error).__name__}: {error}",
                )
                await self._sleep(delay)
                continue
            self._emit(
                job.id,
                "check_succeeded",
                attempt=attempt,
                duration_seconds=f"{self._monotonic() - started_at:.3f}",
                opportunities=len(execution.result.opportunities),
                transitions=len(execution.transitions),
                notifications_delivered=execution.delivery.delivered,
                notifications_failed=execution.delivery.failed,
            )
            if self._operational_alerts is not None:
                await self._operational_alerts.delivery_backlog(
                    alert_after=self._failure_alert_after,
                    channels=self._operational_channels,
                )
            return

    def _seconds_until_next_due(self) -> float:
        if not self._next_due:
            return 3600.0
        return max(0.001, min(self._next_due.values()) - self._monotonic())

    def _discard_completed_tasks(self) -> None:
        self._tasks = {
            job_id: task for job_id, task in self._tasks.items() if not task.done()
        }

    def _emit(self, job_id: str, event: str, **fields: object) -> None:
        parts = [f"job={job_id}", f"event={event}"]
        parts.extend(f"{name}={value}" for name, value in fields.items())
        self._event_sink(" ".join(parts))
