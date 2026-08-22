"""Provider-independent SMTP email notification adapter."""

import asyncio
import os
import smtplib
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr

from web_checker.notifications.models import OperationalAlert, PendingNotification

SMTP_ENVIRONMENT_PREFIX = "WEB_CHECKER_SMTP_"
SMTP_ENVIRONMENT_FIELDS = frozenset(
    {
        f"{SMTP_ENVIRONMENT_PREFIX}HOST",
        f"{SMTP_ENVIRONMENT_PREFIX}PORT",
        f"{SMTP_ENVIRONMENT_PREFIX}SECURITY",
        f"{SMTP_ENVIRONMENT_PREFIX}USERNAME",
        f"{SMTP_ENVIRONMENT_PREFIX}PASSWORD",
        f"{SMTP_ENVIRONMENT_PREFIX}FROM",
        f"{SMTP_ENVIRONMENT_PREFIX}TO",
        f"{SMTP_ENVIRONMENT_PREFIX}TIMEOUT_SECONDS",
    }
)
SMTP_SECURITY_MODES = frozenset({"starttls", "implicit-tls", "none"})


class EmailConfigurationError(Exception):
    """Raised when SMTP environment configuration is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class SMTPSettings:
    """Validated SMTP connection and addressing settings."""

    host: str
    port: int
    security: str
    sender: str
    recipients: tuple[str, ...]
    timeout_seconds: float = 10.0
    username: str | None = None
    password: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "SMTPSettings | None":
        """Return settings when SMTP variables are present, otherwise ``None``."""

        values = environment if environment is not None else os.environ
        present = SMTP_ENVIRONMENT_FIELDS.intersection(values)
        if not present:
            return None

        required = {
            f"{SMTP_ENVIRONMENT_PREFIX}HOST",
            f"{SMTP_ENVIRONMENT_PREFIX}FROM",
            f"{SMTP_ENVIRONMENT_PREFIX}TO",
        }
        missing = sorted(name for name in required if not values.get(name, "").strip())
        if missing:
            raise EmailConfigurationError(
                "incomplete SMTP configuration; missing: " + ", ".join(missing)
            )

        security = values.get(f"{SMTP_ENVIRONMENT_PREFIX}SECURITY", "starttls").strip()
        if security not in SMTP_SECURITY_MODES:
            raise EmailConfigurationError(
                "WEB_CHECKER_SMTP_SECURITY must be one of: "
                + ", ".join(sorted(SMTP_SECURITY_MODES))
            )

        default_ports = {"starttls": 587, "implicit-tls": 465, "none": 25}
        port = _parse_integer(
            values.get(f"{SMTP_ENVIRONMENT_PREFIX}PORT"),
            "WEB_CHECKER_SMTP_PORT",
            default=default_ports[security],
            minimum=1,
            maximum=65535,
        )
        timeout_seconds = _parse_float(
            values.get(f"{SMTP_ENVIRONMENT_PREFIX}TIMEOUT_SECONDS"),
            "WEB_CHECKER_SMTP_TIMEOUT_SECONDS",
            default=10.0,
            minimum=0,
            maximum=120,
        )

        username = _optional_nonblank(values, f"{SMTP_ENVIRONMENT_PREFIX}USERNAME")
        password = _optional_nonblank(values, f"{SMTP_ENVIRONMENT_PREFIX}PASSWORD")
        if (username is None) != (password is None):
            raise EmailConfigurationError(
                "WEB_CHECKER_SMTP_USERNAME and WEB_CHECKER_SMTP_PASSWORD "
                "must be set together"
            )

        sender = _parse_address(
            values[f"{SMTP_ENVIRONMENT_PREFIX}FROM"],
            "WEB_CHECKER_SMTP_FROM",
        )
        recipients = tuple(
            _parse_address(item, "WEB_CHECKER_SMTP_TO")
            for item in values[f"{SMTP_ENVIRONMENT_PREFIX}TO"].split(",")
        )
        if not recipients:
            raise EmailConfigurationError(
                "WEB_CHECKER_SMTP_TO must contain at least one email address"
            )

        return cls(
            host=values[f"{SMTP_ENVIRONMENT_PREFIX}HOST"].strip(),
            port=port,
            security=security,
            sender=sender,
            recipients=recipients,
            timeout_seconds=timeout_seconds,
            username=username,
            password=password,
        )


class EmailNotifier:
    """Sends generic opportunity titles and source links through SMTP."""

    name = "email"

    def __init__(
        self,
        settings: SMTPSettings,
        *,
        smtp_factory: Callable[..., smtplib.SMTP] = smtplib.SMTP,
        smtp_ssl_factory: Callable[..., smtplib.SMTP_SSL] = smtplib.SMTP_SSL,
        ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
    ) -> None:
        self._settings = settings
        self._smtp_factory = smtp_factory
        self._smtp_ssl_factory = smtp_ssl_factory
        self._ssl_context_factory = ssl_context_factory

    async def send(self, notification: PendingNotification | OperationalAlert) -> None:
        """Deliver one notification without blocking the asynchronous worker."""

        message = self._build_message(notification)
        await asyncio.to_thread(self._send_message, message)

    def _build_message(
        self, notification: PendingNotification | OperationalAlert
    ) -> EmailMessage:
        if isinstance(notification, OperationalAlert):
            title = " ".join(notification.title.split())
            content = notification.detail
        else:
            title = " ".join(notification.opportunity_title.split())
            content = title
        message = EmailMessage()
        message["Subject"] = f"[Web Checker] {title}"
        message["From"] = self._settings.sender
        message["To"] = ", ".join(self._settings.recipients)
        lines = [content]
        if (
            isinstance(notification, PendingNotification)
            and notification.booking_url is not None
        ):
            lines.extend(("", notification.booking_url))
        message.set_content("\n".join(lines) + "\n")
        return message

    def _send_message(self, message: EmailMessage) -> None:
        settings = self._settings
        context = self._ssl_context_factory() if settings.security != "none" else None
        if settings.security == "implicit-tls":
            assert context is not None
            client = self._smtp_ssl_factory(
                settings.host,
                settings.port,
                timeout=settings.timeout_seconds,
                context=context,
            )
        else:
            client = self._smtp_factory(
                settings.host,
                settings.port,
                timeout=settings.timeout_seconds,
            )

        with client as smtp:
            if settings.security == "starttls":
                assert context is not None
                smtp.starttls(context=context)
            if settings.username is not None:
                assert settings.password is not None
                smtp.login(settings.username, settings.password)
            smtp.send_message(
                message,
                from_addr=settings.sender,
                to_addrs=settings.recipients,
            )


def _optional_nonblank(values: Mapping[str, str], name: str) -> str | None:
    if name not in values:
        return None
    value = values[name].strip()
    if not value:
        raise EmailConfigurationError(f"{name} must not be blank")
    return value


def _parse_address(value: str, name: str) -> str:
    candidate = value.strip()
    display_name, address = parseaddr(candidate)
    if (
        not candidate
        or display_name
        or address != candidate
        or "@" not in address
        or any(character in candidate for character in "\r\n")
    ):
        raise EmailConfigurationError(
            f"{name} must contain plain email addresses without display names"
        )
    return address


def _parse_integer(
    value: str | None,
    name: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise EmailConfigurationError(f"{name} must be an integer") from error
    if not minimum <= parsed <= maximum:
        raise EmailConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _parse_float(
    value: str | None,
    name: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError as error:
        raise EmailConfigurationError(f"{name} must be a number") from error
    if not minimum < parsed <= maximum:
        raise EmailConfigurationError(
            f"{name} must be greater than {minimum:g} and at most {maximum:g}"
        )
    return parsed
