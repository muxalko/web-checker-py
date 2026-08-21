"""Command-line interface for one-shot checks and scheduled operation."""

import argparse
import asyncio
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from web_checker.config import ApplicationConfig, ConfigurationError, load_config
from web_checker.core.models import Opportunity
from web_checker.core.scheduler import Scheduler
from web_checker.core.service import CheckService
from web_checker.core.transitions import OpportunityTransition
from web_checker.drivers.errors import DriverError
from web_checker.drivers.registry import DriverRegistry, create_default_registry
from web_checker.notifications.registry import (
    NotifierRegistry,
    NotifierRegistryError,
    create_default_notifier_registry,
)
from web_checker.notifications.service import NotificationService
from web_checker.storage import SQLiteObservationStore, StorageError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web-checker")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("jobs.yaml"),
        help="YAML job configuration (default: jobs.yaml)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("web-checker.db"),
        help="SQLite state database (default: web-checker.db)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate-config", help="validate all configured jobs")
    check_parser = subparsers.add_parser("check", help="run one configured job once")
    check_parser.add_argument("job_id")
    subparsers.add_parser("worker", help="run enabled jobs on their schedules")
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    registry: DriverRegistry | None = None,
    notifier_registry: NotifierRegistry | None = None,
) -> int:
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    arguments = build_parser().parse_args(argv)
    selected_registry = registry or create_default_registry()

    try:
        selected_notifiers = notifier_registry or create_default_notifier_registry(
            output
        )
        config = load_config(arguments.config)
        if arguments.command == "validate-config":
            _validate_integrations(config, selected_registry, selected_notifiers)
            print(f"Configuration valid: {len(config.jobs)} job(s)", file=output)
            return 0

        if arguments.command == "worker":
            _validate_integrations(config, selected_registry, selected_notifiers)
            _validate_worker_schedules(config)
            asyncio.run(
                _run_worker(
                    config,
                    arguments.database,
                    selected_registry,
                    selected_notifiers,
                    output,
                )
            )
            return 0

        job = config.get_job(arguments.job_id)
        if not job.enabled:
            raise ConfigurationError(f"job {job.id!r} is disabled")
        store = SQLiteObservationStore(arguments.database)
        service = CheckService(
            selected_registry,
            store,
            NotificationService(store, selected_notifiers),
        )
        execution = asyncio.run(service.check(job))
    except (
        ConfigurationError,
        DriverError,
        NotifierRegistryError,
        StorageError,
    ) as error:
        print(f"Error: {error}", file=errors)
        return 1

    print(f"Job: {job.id}", file=output)
    print(f"Driver: {job.driver}", file=output)
    print(f"Checked at: {execution.result.checked_at.isoformat()}", file=output)
    for opportunity in execution.result.opportunities:
        print(_format_opportunity(opportunity), file=output)
    if execution.baseline_created:
        print("State: baseline recorded", file=output)
    else:
        print(f"Transitions: {len(execution.transitions)}", file=output)
    for transition in execution.transitions:
        print(_format_transition(transition), file=output)
    if execution.delivery.delivered or execution.delivery.failed:
        print(
            f"Notifications: {execution.delivery.delivered} delivered, "
            f"{execution.delivery.failed} pending after failure",
            file=output,
        )
    return 0


def _validate_integrations(
    config: ApplicationConfig,
    registry: DriverRegistry,
    notifier_registry: NotifierRegistry,
) -> None:
    for job in config.jobs:
        registry.get(job.driver).validate_config(job.config)
        if job.enabled:
            for channel in job.notifications.channels:
                notifier_registry.get(channel)


def _validate_worker_schedules(config: ApplicationConfig) -> None:
    enabled_jobs = [job for job in config.jobs if job.enabled]
    if not enabled_jobs:
        raise ConfigurationError("worker requires at least one enabled job")
    missing = [job.id for job in enabled_jobs if job.schedule is None]
    if missing:
        raise ConfigurationError(
            "enabled worker jobs require a schedule: " + ", ".join(missing)
        )


async def _run_worker(
    config: ApplicationConfig,
    database: Path,
    registry: DriverRegistry,
    notifier_registry: NotifierRegistry,
    output: TextIO,
) -> None:
    store = SQLiteObservationStore(database)
    service = CheckService(
        registry,
        store,
        NotificationService(store, notifier_registry),
    )
    stop_event = asyncio.Event()
    scheduler = Scheduler(
        config.jobs,
        service,
        event_sink=lambda event: print(event, file=output, flush=True),
    )
    loop = asyncio.get_running_loop()
    installed_signals = []

    def request_stop(received_signal: signal.Signals) -> None:
        print(
            f"worker event=shutdown_requested signal={received_signal.name}",
            file=output,
            flush=True,
        )
        stop_event.set()

    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(
                shutdown_signal,
                request_stop,
                shutdown_signal,
            )
        except (NotImplementedError, RuntimeError):
            continue
        installed_signals.append(shutdown_signal)

    print(
        "worker event=started scheduled_jobs="
        f"{sum(job.enabled and job.schedule is not None for job in config.jobs)}",
        file=output,
        flush=True,
    )
    try:
        await scheduler.run(stop_event)
    finally:
        for shutdown_signal in installed_signals:
            loop.remove_signal_handler(shutdown_signal)
    print("worker event=stopped", file=output, flush=True)


def _format_opportunity(opportunity: Opportunity) -> str:
    parts = [f"- [{opportunity.availability.value}]", opportunity.title]
    if opportunity.starts_at is not None:
        parts.append(f"at {opportunity.starts_at.isoformat()}")
    if opportunity.booking_url is not None:
        parts.append(f"({opportunity.booking_url})")
    return " ".join(parts)


def _format_transition(transition: OpportunityTransition) -> str:
    opportunity = transition.current or transition.previous
    return f"* {transition.type.value}: {opportunity.title}"


if __name__ == "__main__":
    raise SystemExit(main())
