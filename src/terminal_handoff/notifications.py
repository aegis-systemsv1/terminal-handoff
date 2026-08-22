"""Durable local, webhook and Messages notifications for Terminal Handoff.

The installed macOS runtime remains a single dependency-free file. This module
is a small package-facing facade so integrations can use the notification API
without importing unrelated detector and launcher symbols.
"""

from terminal_handoff.core import (  # noqa: F401
    NOTIFICATION_SCHEMA_VERSION,
    default_notification_config,
    deliver_messages_notification,
    deliver_pending_notification,
    deliver_webhook_notification,
    drain_notification_outbox,
    enqueue_notification,
    launch_failure_notification_event,
    load_notification_config,
    notification_config_path,
    notification_presence,
    reconcile_notification_outbox,
    notification_summary,
    retry_dead_notifications,
    save_notification_config,
    selected_notification_channels,
    set_notification_presence,
    transfer_notification_event,
)

__all__ = [
    "NOTIFICATION_SCHEMA_VERSION",
    "default_notification_config",
    "deliver_messages_notification",
    "deliver_pending_notification",
    "deliver_webhook_notification",
    "drain_notification_outbox",
    "enqueue_notification",
    "launch_failure_notification_event",
    "load_notification_config",
    "notification_config_path",
    "notification_presence",
    "reconcile_notification_outbox",
    "notification_summary",
    "retry_dead_notifications",
    "save_notification_config",
    "selected_notification_channels",
    "set_notification_presence",
    "transfer_notification_event",
]
