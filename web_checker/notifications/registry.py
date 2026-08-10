"""Explicit registry for notification adapters."""

from collections.abc import Iterable

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
