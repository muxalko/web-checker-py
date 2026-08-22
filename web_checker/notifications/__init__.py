"""Notification policies, adapters, and delivery service."""

from web_checker.notifications.base import Notifier
from web_checker.notifications.console import ConsoleNotifier
from web_checker.notifications.email import EmailNotifier, SMTPSettings
from web_checker.notifications.models import (
    NotificationItem,
    NotificationPlan,
    PendingNotification,
)
from web_checker.notifications.registry import (
    NotifierRegistry,
    create_default_notifier_registry,
)
from web_checker.notifications.service import NotificationService

__all__ = [
    "ConsoleNotifier",
    "EmailNotifier",
    "NotificationItem",
    "NotificationPlan",
    "NotificationService",
    "Notifier",
    "NotifierRegistry",
    "PendingNotification",
    "SMTPSettings",
    "create_default_notifier_registry",
]
