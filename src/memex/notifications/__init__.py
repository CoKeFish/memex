"""Notificaciones: el seam `Notifier` + el stub `LoggingNotifier`.

Solo el contrato y el stub por ahora; el servicio real (persistencia + vista en el dashboard) lo
construye otra sesión reemplazando `build_notifier`.
"""

from __future__ import annotations

from memex.notifications.client import Notification, Notifier
from memex.notifications.logging_notifier import LoggingNotifier, build_notifier

__all__ = ["LoggingNotifier", "Notification", "Notifier", "build_notifier"]
