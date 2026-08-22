"""Command-line interface for one-shot checks and scheduled operation."""

import argparse
import asyncio
import os
import signal
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from web_checker.config import ApplicationConfig, ConfigurationError, load_config
from web_checker.core.models import Opportunity
from web_checker.core.scheduler import Scheduler
from web_checker.core.service import CheckService
from web_checker.core.transitions import OpportunityTransition
from web_checker.drivers.errors import DriverError
from web_checker.drivers.registry import DriverRegistry, create_default_registry
from web_checker.maintenance import create_backup, read_backup_status, validate_restore
from web_checker.notifications.operational import OperationalAlertService
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
    maintenance = subparsers.add_parser("maintenance", help="back up and prune state")
    maintenance.add_argument("--backup-directory", type=Path, required=True)
    maintenance.add_argument("--observation-days", type=int, default=90)
    maintenance.add_argument("--notification-days", type=int, default=30)
    maintenance.add_argument("--retain-backups", type=int, default=14)
    maintenance.add_argument("--interval-seconds", type=int, default=86400)
    maintenance.add_argument("--once", action="store_true")
    status = subparsers.add_parser("status", help="show database and backup status")
    status.add_argument("--backup-directory", type=Path)
    status.add_argument(
        "--health",
        action="store_true",
        help="exit nonzero when application health is degraded",
    )
    restore = subparsers.add_parser(
        "validate-restore", help="validate a restored database"
    )
    restore.add_argument("path", type=Path)
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

    try:
        if arguments.command == "maintenance":
            _run_maintenance(arguments, output)
            return 0
        if arguments.command == "status":
            healthy = _print_status(
                arguments.database, arguments.backup_directory, output
            )
            return 0 if healthy or not arguments.health else 2
        if arguments.command == "validate-restore":
            validate_restore(arguments.path)
            print(f"Restore valid: {arguments.path}", file=output)
            return 0

        selected_registry = registry or create_default_registry()
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


def _run_maintenance(arguments: argparse.Namespace, output: TextIO) -> None:
    if arguments.observation_days < 1 or arguments.notification_days < 1:
        raise StorageError("retention days must be at least 1")
    if arguments.interval_seconds < 1:
        raise StorageError("maintenance interval must be at least 1 second")
    while True:
        try:
            _perform_maintenance(arguments, output)
        except StorageError as error:
            if arguments.once:
                raise
            print(f"maintenance event=failed error={error}", file=output, flush=True)
        if arguments.once:
            return
        time.sleep(arguments.interval_seconds)


def _perform_maintenance(arguments: argparse.Namespace, output: TextIO) -> None:
    store = SQLiteObservationStore(arguments.database)
    backup = create_backup(
        arguments.database,
        arguments.backup_directory,
        retain=arguments.retain_backups,
    )
    now = datetime.now(UTC)
    result = store.prune(
        observations_before=now - timedelta(days=arguments.observation_days),
        notifications_before=now - timedelta(days=arguments.notification_days),
    )
    status = store.status()
    oldest = (
        status.oldest_observation.isoformat() if status.oldest_observation else "none"
    )
    print(
        "maintenance event=completed "
        f"backup={backup.path.name} backup_bytes={backup.bytes} "
        f"database_bytes={status.database_bytes} check_runs={status.check_runs} "
        f"oldest_observation={oldest} "
        f"pending_notifications={status.pending_notifications} "
        f"deleted_runs={result.check_runs_deleted} "
        f"deleted_notifications={result.completed_notifications_deleted}",
        file=output,
        flush=True,
    )


def _print_status(
    database: Path, backup_directory: Path | None, output: TextIO
) -> bool:
    status = SQLiteObservationStore(database).status()
    print(f"Database bytes: {status.database_bytes}", file=output)
    print(f"Check runs: {status.check_runs}", file=output)
    print(
        "Oldest observation: "
        + (
            status.oldest_observation.isoformat()
            if status.oldest_observation
            else "none"
        ),
        file=output,
    )
    print(f"Pending notifications: {status.pending_notifications}", file=output)
    jobs = SQLiteObservationStore(database).list_job_statuses()
    healthy = True
    for job in jobs:
        if job.consecutive_failures or job.failed_deliveries:
            healthy = False
        availability = (
            ",".join(
                f"{name}:{count}" for name, count in sorted(job.availability.items())
            )
            or "none"
        )
        last_failure = (
            job.last_failure_at.isoformat() if job.last_failure_at else "none"
        )
        print(
            f"Job {job.job_id}: last_success="
            f"{job.last_success_at.isoformat() if job.last_success_at else 'none'} "
            f"last_failure={last_failure} "
            f"consecutive_failures={job.consecutive_failures} "
            f"availability={availability} pending_deliveries={job.pending_deliveries} "
            f"failed_deliveries={job.failed_deliveries}",
            file=output,
        )
        if job.last_error is not None:
            print(f"  Last error: {job.last_error}", file=output)
    if backup_directory is not None:
        backup = read_backup_status(backup_directory)
        print(
            "Last backup: "
            + (
                f"{backup.completed_at.isoformat()} "
                f"({backup.path}, {backup.bytes} bytes)"
                if backup
                else "none"
            ),
            file=output,
        )
    return healthy


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
    default_operational_channels = (
        "console,email" if "email" in notifier_registry.names() else "console"
    )
    operational_channels = tuple(
        item.strip()
        for item in os.environ.get(
            "WEB_CHECKER_OPERATIONAL_CHANNELS", default_operational_channels
        ).split(",")
        if item.strip()
    )
    if not operational_channels:
        raise ConfigurationError("WEB_CHECKER_OPERATIONAL_CHANNELS must not be empty")
    try:
        failure_alert_after = int(
            os.environ.get("WEB_CHECKER_FAILURE_ALERT_AFTER", "3")
        )
    except ValueError as error:
        raise ConfigurationError(
            "WEB_CHECKER_FAILURE_ALERT_AFTER must be an integer"
        ) from error
    if failure_alert_after < 1:
        raise ConfigurationError("WEB_CHECKER_FAILURE_ALERT_AFTER must be at least 1")
    for channel in operational_channels:
        notifier_registry.get(channel)
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
        operational_alerts=OperationalAlertService(
            store,
            notifier_registry,
            event_sink=lambda event: print(event, file=output, flush=True),
        ),
        failure_alert_after=failure_alert_after,
        operational_channels=operational_channels,
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
