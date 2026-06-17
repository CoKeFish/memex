"""Stub de `Notifier`: deja el aviso en el log (structlog). Verifica el seam sin persistir nada.

`build_notifier()` es el ÚNICO punto de cableado del notificador activo: hoy devuelve
`LoggingNotifier`; cuando exista el servicio de notificaciones real (persistencia + vista en el
dashboard), otra sesión cambia SOLO esta función y ni los emisores (transport) ni el scheduler se
tocan.
"""

from __future__ import annotations

from memex.logging import get_logger
from memex.notifications.client import Notification, Notifier

_log = get_logger("memex.notifications")


class LoggingNotifier:
    """`Notifier` que solo deja rastro en el log — stub mientras no exista el servicio real."""

    async def notify(self, notification: Notification) -> None:
        _log.info(
            "notification.emitted",
            kind=notification.kind,
            severity=notification.severity,
            dedup_key=notification.dedup_key,
            title=notification.title,
            payload=notification.payload,
        )


def build_notifier() -> Notifier:
    """El `Notifier` activo. Hoy: el stub que loguea. Mañana: el servicio real (otra sesión)."""
    return LoggingNotifier()
