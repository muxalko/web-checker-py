import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace

from web_checker.config import JobDefinition, RetryPolicy, ScheduleDefinition
from web_checker.core.scheduler import Scheduler


@dataclass
class FakeClock:
    value: float = 0.0

    def __call__(self):
        return self.value


@dataclass
class SuccessfulRunner:
    calls: list[str] = field(default_factory=list)

    async def check(self, job):
        self.calls.append(job.id)
        return execution()


def job(job_id="one", *, interval=10, retry=None, enabled=True):
    return JobDefinition(
        id=job_id,
        driver="fake",
        config={},
        enabled=enabled,
        schedule=ScheduleDefinition(
            interval_seconds=interval,
            retry=retry or RetryPolicy(),
        ),
    )


def execution():
    return SimpleNamespace(
        result=SimpleNamespace(opportunities=()),
        transitions=(),
        delivery=SimpleNamespace(delivered=0, failed=0),
    )


def test_due_jobs_run_immediately_then_at_their_own_intervals():
    async def scenario():
        clock = FakeClock()
        runner = SuccessfulRunner()
        scheduler = Scheduler(
            [job("fast", interval=5), job("slow", interval=20)],
            runner,
            monotonic=clock,
        )

        await scheduler.tick()
        await scheduler.wait_for_idle()
        clock.value = 5
        await scheduler.tick()
        await scheduler.wait_for_idle()

        assert runner.calls == ["fast", "slow", "fast"]

    asyncio.run(scenario())


def test_disabled_and_unscheduled_jobs_are_not_run():
    async def scenario():
        runner = SuccessfulRunner()
        unscheduled = JobDefinition(id="manual", driver="fake", config={})
        scheduler = Scheduler([job("disabled", enabled=False), unscheduled], runner)

        await scheduler.tick()
        await scheduler.wait_for_idle()

        assert runner.calls == []

    asyncio.run(scenario())


def test_overlapping_run_of_the_same_job_is_skipped():
    async def scenario():
        clock = FakeClock()
        started = asyncio.Event()
        release = asyncio.Event()
        events = []

        class BlockingRunner:
            calls = 0

            async def check(self, selected_job):
                self.calls += 1
                started.set()
                await release.wait()
                return execution()

        runner = BlockingRunner()
        scheduler = Scheduler(
            [job(interval=5)],
            runner,
            monotonic=clock,
            event_sink=events.append,
        )

        await scheduler.tick()
        await started.wait()
        clock.value = 5
        await scheduler.tick()

        assert runner.calls == 1
        assert scheduler.running_job_ids == {"one"}
        assert any("event=overlap_skipped" in event for event in events)

        release.set()
        await scheduler.wait_for_idle()

    asyncio.run(scenario())


def test_failures_are_retried_with_exponential_backoff_and_do_not_block_jobs():
    async def scenario():
        delays = []
        events = []

        async def fake_sleep(delay):
            delays.append(delay)

        class FlakyRunner:
            def __init__(self):
                self.attempts = {}

            async def check(self, selected_job):
                count = self.attempts.get(selected_job.id, 0) + 1
                self.attempts[selected_job.id] = count
                if selected_job.id == "flaky" and count < 3:
                    raise RuntimeError("temporary")
                return execution()

        retry = RetryPolicy(
            max_attempts=3,
            initial_delay_seconds=2,
            max_delay_seconds=10,
            jitter_fraction=0,
        )
        runner = FlakyRunner()
        scheduler = Scheduler(
            [job("flaky", retry=retry), job("healthy", retry=retry)],
            runner,
            sleep=fake_sleep,
            random_unit=lambda: 0.5,
            event_sink=events.append,
        )

        await scheduler.tick()
        await scheduler.wait_for_idle()

        assert runner.attempts == {"flaky": 3, "healthy": 1}
        assert delays == [2, 4]
        assert sum("job=flaky event=retry_scheduled" in event for event in events) == 2
        assert any("job=healthy event=check_succeeded" in event for event in events)

    asyncio.run(scenario())


def test_retry_limit_is_bounded_and_failure_is_contained():
    async def scenario():
        events = []

        class FailingRunner:
            calls = 0

            async def check(self, selected_job):
                self.calls += 1
                raise ValueError("still broken")

        runner = FailingRunner()
        scheduler = Scheduler(
            [
                job(
                    retry=RetryPolicy(
                        max_attempts=2,
                        initial_delay_seconds=0.1,
                        max_delay_seconds=0.1,
                        jitter_fraction=0,
                    )
                )
            ],
            runner,
            sleep=lambda delay: asyncio.sleep(0),
            event_sink=events.append,
        )

        await scheduler.tick()
        await scheduler.wait_for_idle()

        assert runner.calls == 2
        assert any("event=check_failed" in event for event in events)

    asyncio.run(scenario())


def test_terminal_failure_is_reported_to_operational_alerts():
    async def scenario():
        class FailingRunner:
            async def check(self, selected_job):
                raise ValueError("provider broken")

        class Alerts:
            def __init__(self):
                self.failures = []

            async def check_failed(self, job_id, error, **settings):
                self.failures.append((job_id, error, settings))

            async def dispatch_pending(self):
                pass

            async def delivery_backlog(self, **settings):
                pass

        alerts = Alerts()
        scheduler = Scheduler(
            [job(retry=RetryPolicy(max_attempts=1))],
            FailingRunner(),
            operational_alerts=alerts,
            failure_alert_after=2,
            operational_channels=("email",),
        )

        await scheduler.tick()
        await scheduler.wait_for_idle()

        assert alerts.failures == [
            (
                "one",
                "ValueError: provider broken",
                {"alert_after": 2, "channels": ("email",)},
            )
        ]

    asyncio.run(scenario())


def test_shutdown_cancels_a_check_after_the_grace_period():
    async def scenario():
        cancelled = asyncio.Event()
        events = []

        class BlockingRunner:
            async def check(self, selected_job):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        scheduler = Scheduler(
            [job()],
            BlockingRunner(),
            shutdown_timeout_seconds=0,
            event_sink=events.append,
        )
        await scheduler.tick()

        await scheduler.shutdown()

        assert cancelled.is_set()
        assert scheduler.running_job_ids == set()
        assert any("event=shutdown_cancelled" in event for event in events)

    asyncio.run(scenario())
