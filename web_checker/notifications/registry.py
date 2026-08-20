"""Explicit registry and environment-backed defaults for notification adapters."""

import os
from collections.abc import Iterable, Mapping
from typing import TextIO

from web_checker.notifications.base import Notifier


class NotifierRegistryError(Exception):
    """Base class for notification registry errors."""


class UnknownNotifierError(NotifierRegistryError):
    """Raised when a job references an unregistered channel."""


class NotifierRegistry:
    """Stores notifier instances by stable channel name."""

    def __init__(self, notifiers: Iterable[Notifier] = ()) -> None:
        self._notifiers: dict[str, Notifier] = {}
        for notifier in notifiers:
            self.register(notifier)

    def register(self, notifier: Notifier) -> None:
        if not isinstance(notifier, Notifier):
            raise NotifierRegistryError("Notifier does not implement the contract")
        name = notifier.name.strip()
        if not name:
            raise NotifierRegistryError("Notifier name must not be blank")
        if name in self._notifiers:
            raise NotifierRegistryError(f"Notifier {name!r} is already registered")
        self._notifiers[name] = notifier

    def get(self, name: str) -> Notifier:
        try:
            return self._notifiers[name]
        except KeyError as error:
            available = ", ".join(self.names()) or "none"
            raise UnknownNotifierError(
                f"Unknown notification channel {name!r}; registered channels: "
                f"{available}"
            ) from error

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._notifiers))


def create_default_notifier_registry(
    output: TextIO | None = None,
    environment: Mapping[str, str] | None = None,
) -> NotifierRegistry:
    """Register console and any completely configured external adapters."""

    from web_checker.notifications.console import ConsoleNotifier
    from web_checker.notifications.email import (
        EmailConfigurationError,
        EmailNotifier,
        SMTPSettings,
    )

    values = environment if environment is not None else os.environ
    notifiers: list[Notifier] = [ConsoleNotifier(output)]
    try:
        smtp_settings = SMTPSettings.from_environment(values)
    except EmailConfigurationError as error:
        raise NotifierRegistryError(str(error)) from error
    if smtp_settings is not None:
        notifiers.append(EmailNotifier(smtp_settings))
    return NotifierRegistry(notifiers)
