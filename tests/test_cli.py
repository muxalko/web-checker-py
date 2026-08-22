from datetime import UTC, datetime
from io import StringIO

import httpx

from web_checker.cli import main
from web_checker.drivers.generic_html.driver import GenericHtmlDriver
from web_checker.drivers.registry import DriverRegistry
from web_checker.notifications.email import SMTP_ENVIRONMENT_FIELDS
from web_checker.storage import SQLiteObservationStore


def write_job_config(
    tmp_path,
    *,
    enabled=True,
    driver="generic_html",
    notifications=False,
    scheduled=False,
):
    notification_block = ""
    if notifications:
        notification_block = """
    notify:
      channels: [console]
      on: [became_available]
"""
    schedule_block = ""
    if scheduled:
        schedule_block = """
    schedule:
      interval_seconds: 30
"""
    text = f"""
jobs:
  - id: mock-passes
    enabled: {str(enabled).lower()}
    driver: {driver}
{notification_block.rstrip()}
{schedule_block.rstrip()}
    config:
      url: http://provider.test/reservations
      parser:
        opportunity_selector: "[data-opportunity-id]"
        id:
          attribute: data-opportunity-id
        title:
          selector: ".title"
        availability:
          selector: ".status"
          attribute: data-status
        availability_mapping:
          available: available
          sold-out: unavailable
          unknown: unknown
        starts_at:
          selector: time
          attribute: datetime
        booking_url:
          selector: "a.book"
          attribute: href
          required: false
"""
    path = tmp_path / "jobs.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def mock_registry(fixture_html, status_code=200):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            status_code,
            text=fixture_html("reservations.html") if status_code == 200 else "",
        )
    )
    driver = GenericHtmlDriver(
        transport=transport,
        clock=lambda: datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )
    return DriverRegistry([driver])


def invoke(arguments, registry):
    stdout = StringIO()
    stderr = StringIO()
    exit_code = main(arguments, stdout=stdout, stderr=stderr, registry=registry)
    return exit_code, stdout.getvalue(), stderr.getvalue()


def arguments(path, *command):
    return [
        "--config",
        str(path),
        "--database",
        str(path.with_suffix(".db")),
        *command,
    ]


def test_validate_config_validates_driver_specific_rules(tmp_path, fixture_html):
    path = write_job_config(tmp_path)

    exit_code, stdout, stderr = invoke(
        arguments(path, "validate-config"),
        mock_registry(fixture_html),
    )

    assert exit_code == 0
    assert stdout == "Configuration valid: 1 job(s)\n"
    assert stderr == ""


def test_example_configuration_validates_with_shipped_integrations():
    stdout = StringIO()
    stderr = StringIO()

    exit_code = main(
        ["--config", "jobs.example.yaml", "validate-config"],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Configuration valid: 3 job(s)\n"
    assert stderr.getvalue() == ""


def test_check_prints_normalized_opportunities(tmp_path, fixture_html):
    path = write_job_config(tmp_path)

    exit_code, stdout, stderr = invoke(
        arguments(path, "check", "mock-passes"),
        mock_registry(fixture_html),
    )

    assert exit_code == 0
    assert "Job: mock-passes" in stdout
    assert "Driver: generic_html" in stdout
    assert "Checked at: 2026-08-10T12:00:00+00:00" in stdout
    assert "[available] Morning Adventure Pass" in stdout
    assert "[unavailable] Midday Adventure Pass" in stdout
    assert "http://provider.test/book/morning-pass" in stdout
    assert "State: baseline recorded" in stdout
    assert "* initially_available: Morning Adventure Pass" in stdout
    assert stderr == ""


def test_check_rejects_disabled_job(tmp_path, fixture_html):
    path = write_job_config(tmp_path, enabled=False)

    exit_code, stdout, stderr = invoke(
        arguments(path, "check", "mock-passes"),
        mock_registry(fixture_html),
    )

    assert exit_code == 1
    assert stdout == ""
    assert "is disabled" in stderr


def test_unknown_driver_returns_clean_error(tmp_path, fixture_html):
    path = write_job_config(tmp_path, driver="missing")

    exit_code, stdout, stderr = invoke(
        arguments(path, "validate-config"),
        mock_registry(fixture_html),
    )

    assert exit_code == 1
    assert stdout == ""
    assert "Unknown driver 'missing'" in stderr


def test_disabled_job_may_reference_an_unconfigured_notification_channel(
    tmp_path, fixture_html
):
    path = write_job_config(tmp_path, enabled=False, notifications=True)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "channels: [console]", "channels: [email]"
        ),
        encoding="utf-8",
    )

    exit_code, stdout, stderr = invoke(
        arguments(path, "validate-config"),
        mock_registry(fixture_html),
    )

    assert exit_code == 0
    assert stdout == "Configuration valid: 1 job(s)\n"
    assert stderr == ""


def test_enabled_job_rejects_an_unconfigured_notification_channel(
    tmp_path, fixture_html
):
    path = write_job_config(tmp_path, notifications=True)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "channels: [console]", "channels: [email]"
        ),
        encoding="utf-8",
    )

    exit_code, stdout, stderr = invoke(
        arguments(path, "validate-config"),
        mock_registry(fixture_html),
    )

    assert exit_code == 1
    assert stdout == ""
    assert "Unknown notification channel 'email'" in stderr


def test_partial_smtp_environment_returns_a_clean_error(
    tmp_path, fixture_html, monkeypatch
):
    for name in SMTP_ENVIRONMENT_FIELDS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("WEB_CHECKER_SMTP_HOST", "smtp.example.test")
    path = write_job_config(tmp_path)

    exit_code, stdout, stderr = invoke(
        arguments(path, "validate-config"),
        mock_registry(fixture_html),
    )

    assert exit_code == 1
    assert stdout == ""
    assert "incomplete SMTP configuration" in stderr
    assert "WEB_CHECKER_SMTP_FROM" in stderr
    assert "WEB_CHECKER_SMTP_TO" in stderr


def test_driver_failure_returns_clean_error(tmp_path, fixture_html):
    path = write_job_config(tmp_path)

    exit_code, stdout, stderr = invoke(
        arguments(path, "check", "mock-passes"),
        mock_registry(fixture_html, status_code=503),
    )

    assert exit_code == 1
    assert stdout == ""
    assert "HTTP 503" in stderr


def test_worker_requires_schedules_for_enabled_jobs(tmp_path, fixture_html):
    path = write_job_config(tmp_path)

    exit_code, stdout, stderr = invoke(
        arguments(path, "worker"),
        mock_registry(fixture_html),
    )

    assert exit_code == 1
    assert stdout == ""
    assert "require a schedule: mock-passes" in stderr


def test_worker_wires_valid_scheduled_configuration(
    tmp_path, fixture_html, monkeypatch
):
    path = write_job_config(tmp_path, scheduled=True)
    captured = {}

    async def fake_run_worker(config, database, registry, notifier_registry, output):
        captured["config"] = config
        captured["database"] = database

    monkeypatch.setattr("web_checker.cli._run_worker", fake_run_worker)

    exit_code, stdout, stderr = invoke(
        arguments(path, "worker"),
        mock_registry(fixture_html),
    )

    assert exit_code == 0
    assert captured["config"].jobs[0].schedule.interval_seconds == 30
    assert captured["database"] == path.with_suffix(".db")
    assert stdout == ""
    assert stderr == ""


def test_repeated_check_reports_persisted_availability_transition(
    tmp_path, fixture_html
):
    path = write_job_config(tmp_path)
    command = arguments(path, "check", "mock-passes")
    invoke(command, mock_registry(fixture_html))
    available_html = fixture_html("reservations.html").replace(
        'data-status="sold-out"', 'data-status="available"', 1
    )
    driver = GenericHtmlDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=available_html)
        ),
        clock=lambda: datetime(2026, 8, 10, 12, 1, tzinfo=UTC),
    )

    exit_code, stdout, stderr = invoke(command, DriverRegistry([driver]))

    assert exit_code == 0
    assert "Transitions: 1" in stdout
    assert "* became_available: Midday Adventure Pass" in stdout
    assert stderr == ""


def test_matching_transition_is_delivered_once(tmp_path, fixture_html):
    path = write_job_config(tmp_path, notifications=True)
    command = arguments(path, "check", "mock-passes")
    invoke(command, mock_registry(fixture_html))
    available_html = fixture_html("reservations.html").replace(
        'data-status="sold-out"', 'data-status="available"', 1
    )
    driver = GenericHtmlDriver(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=available_html)
        ),
        clock=lambda: datetime(2026, 8, 10, 12, 1, tzinfo=UTC),
    )

    _, second_output, _ = invoke(command, DriverRegistry([driver]))
    _, third_output, _ = invoke(command, DriverRegistry([driver]))

    assert second_output.count("Notification: mock-passes became_available") == 1
    assert "Notifications: 1 delivered, 0 pending after failure" in second_output
    assert "Notification:" not in third_output


def test_selected_initial_availability_is_delivered_once(tmp_path, fixture_html):
    path = write_job_config(tmp_path, notifications=True)
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "on: [became_available]", "on: [initially_available]"
        ),
        encoding="utf-8",
    )
    command = arguments(path, "check", "mock-passes")

    _, first_output, _ = invoke(command, mock_registry(fixture_html))
    _, second_output, _ = invoke(command, mock_registry(fixture_html))

    assert first_output.count("Notification: mock-passes initially_available") == 1
    assert "Notifications: 1 delivered, 0 pending after failure" in first_output
    assert "Notification:" not in second_output


def test_status_health_distinguishes_degraded_application(tmp_path):
    database = tmp_path / "state.db"
    SQLiteObservationStore(database).record_check_failure(
        "broken-job", "provider unavailable"
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = main(
        ["--database", str(database), "status", "--health"],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 2
    assert "Job broken-job:" in stdout.getvalue()
    assert "consecutive_failures=1" in stdout.getvalue()
    assert stderr.getvalue() == ""
