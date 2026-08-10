from types import MappingProxyType

import pytest

from web_checker.config import ConfigurationError, RetryPolicy, load_config

VALID_CONFIG = """
jobs:
  - id: morning-pass
    driver: generic_html
    config:
      url: http://provider.test/page
      nested:
        value: example
"""


def write_config(tmp_path, content=VALID_CONFIG):
    path = tmp_path / "jobs.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_common_job_fields_and_freezes_nested_config(tmp_path):
    config = load_config(write_config(tmp_path))

    assert len(config.jobs) == 1
    job = config.get_job("morning-pass")
    assert job.enabled is True
    assert job.driver == "generic_html"
    assert isinstance(job.config, MappingProxyType)
    assert isinstance(job.config["nested"], MappingProxyType)
    with pytest.raises(TypeError):
        job.config["url"] = "changed"


def test_disabled_job_is_loaded(tmp_path):
    path = write_config(
        tmp_path, VALID_CONFIG.replace("driver:", "enabled: false\n    driver:")
    )

    assert load_config(path).jobs[0].enabled is False


def test_notification_policy_is_normalized(tmp_path):
    content = (
        VALID_CONFIG
        + """
    notify:
      channels: [console]
      on: [became_available, appeared]
"""
    )
    job = load_config(write_config(tmp_path, content)).jobs[0]

    assert job.notifications.channels == ("console",)
    assert {item.value for item in job.notifications.on} == {
        "became_available",
        "appeared",
    }


def test_schedule_and_retry_policy_are_normalized(tmp_path):
    content = (
        VALID_CONFIG
        + """
    schedule:
      interval_seconds: 30
      retry:
        max_attempts: 4
        initial_delay_seconds: 2
        max_delay_seconds: 20
        jitter_fraction: 0.1
"""
    )

    schedule = load_config(write_config(tmp_path, content)).jobs[0].schedule

    assert schedule is not None
    assert schedule.interval_seconds == 30
    assert schedule.retry == RetryPolicy(
        max_attempts=4,
        initial_delay_seconds=2,
        max_delay_seconds=20,
        jitter_fraction=0.1,
    )


def test_schedule_uses_bounded_retry_defaults(tmp_path):
    content = VALID_CONFIG + "    schedule: {interval_seconds: 60}\n"

    schedule = load_config(write_config(tmp_path, content)).jobs[0].schedule

    assert schedule is not None
    assert schedule.retry == RetryPolicy()


@pytest.mark.parametrize(
    "schedule, message",
    [
        ("schedule: 10", "schedule must be a mapping"),
        ("schedule: {}", "missing.*schedule"),
        ("schedule: {interval_seconds: 0}", "greater than"),
        ("schedule: {interval_seconds: true}", "finite number"),
        ("schedule: {interval_seconds: 10, typo: 1}", "unsupported.*schedule"),
        (
            "schedule: {interval_seconds: 10, retry: value}",
            "retry must be a mapping",
        ),
        (
            "schedule: {interval_seconds: 10, retry: {max_attempts: 0}}",
            "max_attempts",
        ),
        (
            "schedule: {interval_seconds: 10, retry: "
            "{max_attempts: 2, initial_delay_seconds: 0}}",
            "initial_delay_seconds",
        ),
        (
            "schedule: {interval_seconds: 10, retry: {jitter_fraction: 1.1}}",
            "jitter_fraction",
        ),
        (
            "schedule: {interval_seconds: 10, retry: "
            "{initial_delay_seconds: 10, max_delay_seconds: 5}}",
            "max_delay_seconds",
        ),
    ],
)
def test_invalid_schedule_is_rejected(tmp_path, schedule, message):
    content = VALID_CONFIG + f"    {schedule}\n"

    with pytest.raises(ConfigurationError, match=message):
        load_config(write_config(tmp_path, content))


def test_retry_delay_is_exponential_capped_and_jittered():
    retry = RetryPolicy(
        max_attempts=4,
        initial_delay_seconds=10,
        max_delay_seconds=15,
        jitter_fraction=0.2,
    )

    assert retry.delay_after_failure(1, 0.0) == 8
    assert retry.delay_after_failure(2, 0.5) == 15
    assert retry.delay_after_failure(3, 1.0) == 15


@pytest.mark.parametrize(
    "notify, message",
    [
        ("notify: console", "notify must be a mapping"),
        ("notify: {channels: [console]}", "missing.*notify"),
        ("notify: {channels: [], on: [became_available]}", "non-empty"),
        ("notify: {channels: [console], on: [not-real]}", "unknown transition"),
        (
            "notify: {channels: [console, console], on: [became_available]}",
            "channels must be unique",
        ),
    ],
)
def test_invalid_notification_policy_is_rejected(tmp_path, notify, message):
    content = VALID_CONFIG + f"    {notify}\n"

    with pytest.raises(ConfigurationError, match=message):
        load_config(write_config(tmp_path, content))


def test_duplicate_job_ids_are_rejected(tmp_path):
    duplicate = VALID_CONFIG + VALID_CONFIG.split("jobs:\n", 1)[1]

    with pytest.raises(ConfigurationError, match="duplicate job ids"):
        load_config(write_config(tmp_path, duplicate))


@pytest.mark.parametrize(
    "content, message",
    [
        ("[]", "root must be a mapping"),
        ("jobs: []", "non-empty list"),
        ("jobs: [value]", r"jobs\[0\] must be a mapping"),
        ("jobs:\n  - id: bad id\n    driver: x\n    config: {}", "job id"),
        ("jobs:\n  - id: one\n    driver: x", "missing jobs"),
        (
            "jobs:\n  - id: one\n    driver: x\n    config: {}\n    typo: x",
            "unsupported",
        ),
        ("jobs: {}\nunknown: true", "unsupported root"),
    ],
)
def test_invalid_common_configuration_is_rejected(tmp_path, content, message):
    with pytest.raises(ConfigurationError, match=message):
        load_config(write_config(tmp_path, content))


def test_unsafe_yaml_tag_is_rejected(tmp_path):
    path = write_config(tmp_path, "!!python/object/apply:os.system ['echo unsafe']")

    with pytest.raises(ConfigurationError, match="invalid YAML"):
        load_config(path)


def test_missing_file_has_context(tmp_path):
    with pytest.raises(ConfigurationError, match="could not read"):
        load_config(tmp_path / "missing.yaml")


def test_unknown_job_lists_available_jobs(tmp_path):
    config = load_config(write_config(tmp_path))

    with pytest.raises(ConfigurationError, match="configured jobs: morning-pass"):
        config.get_job("missing")
