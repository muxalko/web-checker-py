import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from web_checker.core.models import Availability
from web_checker.core.transitions import TransitionType
from web_checker.notifications.email import (
    EmailConfigurationError,
    EmailNotifier,
    SMTPSettings,
)
from web_checker.notifications.models import PendingNotification
from web_checker.notifications.registry import (
    NotifierRegistryError,
    create_default_notifier_registry,
)

SMTP_ENVIRONMENT = {
    "WEB_CHECKER_SMTP_HOST": "smtp.example.test",
    "WEB_CHECKER_SMTP_PORT": "587",
    "WEB_CHECKER_SMTP_SECURITY": "starttls",
    "WEB_CHECKER_SMTP_USERNAME": "mailer",
    "WEB_CHECKER_SMTP_PASSWORD": "not-a-real-secret",
    "WEB_CHECKER_SMTP_FROM": "alerts@example.test",
    "WEB_CHECKER_SMTP_TO": "one@example.test,two@example.test",
    "WEB_CHECKER_SMTP_TIMEOUT_SECONDS": "12.5",
}


def pending(channel="email"):
    return PendingNotification(
        id=1,
        channel=channel,
        job_id="job",
        transition_type=TransitionType.BECAME_AVAILABLE,
        opportunity_id="pass",
        opportunity_title="Morning Pass",
        current_availability=Availability.AVAILABLE,
        booking_url="https://example.test/book",
        checked_at=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
        attempts=0,
    )


class FakeSMTP:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.starttls_context = None
        self.login_credentials = None
        self.message = None
        self.envelope = None
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.closed = True

    def starttls(self, *, context):
        self.starttls_context = context

    def login(self, username, password):
        self.login_credentials = (username, password)

    def send_message(self, message, *, from_addr, to_addrs):
        self.message = message
        self.envelope = (from_addr, to_addrs)


class SMTPFactory:
    def __init__(self):
        self.instances = []

    def __call__(self, *args, **kwargs):
        instance = FakeSMTP(*args, **kwargs)
        self.instances.append(instance)
        return instance


def test_smtp_settings_are_absent_when_environment_is_unconfigured():
    assert SMTPSettings.from_environment({}) is None


def test_smtp_settings_parse_complete_environment_without_exposing_password():
    settings = SMTPSettings.from_environment(SMTP_ENVIRONMENT)

    assert settings == SMTPSettings(
        host="smtp.example.test",
        port=587,
        security="starttls",
        sender="alerts@example.test",
        recipients=("one@example.test", "two@example.test"),
        timeout_seconds=12.5,
        username="mailer",
        password="not-a-real-secret",
    )
    assert "not-a-real-secret" not in repr(settings)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        (
            {"WEB_CHECKER_SMTP_HOST": ""},
            "incomplete SMTP configuration.*WEB_CHECKER_SMTP_HOST",
        ),
        ({"WEB_CHECKER_SMTP_PORT": "zero"}, "SMTP_PORT must be an integer"),
        ({"WEB_CHECKER_SMTP_PORT": "0"}, "SMTP_PORT must be between"),
        ({"WEB_CHECKER_SMTP_SECURITY": "maybe"}, "SMTP_SECURITY must be"),
        ({"WEB_CHECKER_SMTP_TIMEOUT_SECONDS": "0"}, "must be greater than"),
        ({"WEB_CHECKER_SMTP_TO": "not-an-address"}, "plain email addresses"),
        (
            {"WEB_CHECKER_SMTP_USERNAME": ""},
            "WEB_CHECKER_SMTP_USERNAME must not be blank",
        ),
    ],
)
def test_smtp_settings_reject_invalid_environment(changes, message):
    environment = SMTP_ENVIRONMENT | changes

    with pytest.raises(EmailConfigurationError, match=message):
        SMTPSettings.from_environment(environment)


def test_smtp_settings_require_username_and_password_together():
    environment = {
        key: value
        for key, value in SMTP_ENVIRONMENT.items()
        if key != "WEB_CHECKER_SMTP_PASSWORD"
    }

    with pytest.raises(EmailConfigurationError, match="must be set together"):
        SMTPSettings.from_environment(environment)


def test_default_registry_adds_email_only_when_smtp_is_configured():
    assert create_default_notifier_registry(environment={}).names() == ("console",)
    assert create_default_notifier_registry(environment=SMTP_ENVIRONMENT).names() == (
        "console",
        "email",
    )

    with pytest.raises(NotifierRegistryError, match="incomplete SMTP configuration"):
        create_default_notifier_registry(
            environment={"WEB_CHECKER_SMTP_HOST": "smtp.example.test"}
        )


def test_email_notifier_sends_title_and_link_over_authenticated_starttls():
    settings = SMTPSettings.from_environment(SMTP_ENVIRONMENT)
    assert settings is not None
    smtp_factory = SMTPFactory()
    smtp_ssl_factory = SMTPFactory()
    context = object()
    notifier = EmailNotifier(
        settings,
        smtp_factory=smtp_factory,
        smtp_ssl_factory=smtp_ssl_factory,
        ssl_context_factory=lambda: context,
    )

    asyncio.run(notifier.send(pending(channel="email")))

    assert smtp_ssl_factory.instances == []
    smtp = smtp_factory.instances[0]
    assert smtp.args == ("smtp.example.test", 587)
    assert smtp.kwargs == {"timeout": 12.5}
    assert smtp.starttls_context is context
    assert smtp.login_credentials == ("mailer", "not-a-real-secret")
    assert smtp.envelope == (
        "alerts@example.test",
        ("one@example.test", "two@example.test"),
    )
    assert smtp.message["Subject"] == "[Web Checker] Morning Pass"
    assert smtp.message["From"] == "alerts@example.test"
    assert smtp.message["To"] == "one@example.test, two@example.test"
    assert smtp.message.get_content() == ("Morning Pass\n\nhttps://example.test/book\n")
    assert smtp.closed is True


def test_email_notifier_supports_implicit_tls_without_authentication():
    settings = SMTPSettings(
        host="smtp.example.test",
        port=465,
        security="implicit-tls",
        sender="alerts@example.test",
        recipients=("operator@example.test",),
    )
    smtp_factory = SMTPFactory()
    smtp_ssl_factory = SMTPFactory()
    context = object()
    notifier = EmailNotifier(
        settings,
        smtp_factory=smtp_factory,
        smtp_ssl_factory=smtp_ssl_factory,
        ssl_context_factory=lambda: context,
    )

    asyncio.run(notifier.send(pending(channel="email")))

    assert smtp_factory.instances == []
    smtp = smtp_ssl_factory.instances[0]
    assert smtp.args == ("smtp.example.test", 465)
    assert smtp.kwargs == {"timeout": 10.0, "context": context}
    assert smtp.starttls_context is None
    assert smtp.login_credentials is None


def test_email_notifier_supports_plain_local_capture_and_missing_link():
    settings = SMTPSettings(
        host="mailpit",
        port=1025,
        security="none",
        sender="alerts@example.test",
        recipients=("operator@example.test",),
    )
    smtp_factory = SMTPFactory()
    notification = replace(pending(), booking_url=None)
    notifier = EmailNotifier(
        settings,
        smtp_factory=smtp_factory,
        ssl_context_factory=lambda: pytest.fail(
            "plain local SMTP must not create a TLS context"
        ),
    )

    asyncio.run(notifier.send(notification))

    smtp = smtp_factory.instances[0]
    assert smtp.starttls_context is None
    assert smtp.login_credentials is None
    assert smtp.message.get_content() == "Morning Pass\n"
