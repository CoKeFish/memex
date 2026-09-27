"""Notificaciones: el seam `Notifier`, sus implementaciones y `build_notifier`.

`build_notifier()` cablea el notificador activo (hoy `PersistentNotifier`, que persiste en la cola
`notifications`). El contrato es de uso general: cualquier emisor entrega su `Notification` por el
seam. La capa de datos vive en `store.py`.
"""

from __future__ import annotations

from memex.notifications.client import Notification, Notifier
from memex.notifications.logging_notifier import LoggingNotifier, build_notifier
from memex.notifications.persistent_notifier import PersistentNotifier

__all__ = [
    "LoggingNotifier",
    "Notification",
    "Notifier",
    "PersistentNotifier",
    "build_notifier",
]
