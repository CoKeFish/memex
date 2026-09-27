"""Stub `LoggingNotifier` (solo loguea) + `build_notifier()`, el cableado del notificador activo.

`build_notifier()` es el ÚNICO punto de cableado: hoy devuelve el servicio real
(`PersistentNotifier`, que persiste en la cola `notifications`). `LoggingNotifier` queda como stub
válido — útil en tests o para volver al comportamiento solo-log. Cambiar el notificador activo es
cambiar SOLO esta función; ni los emisores (transport) ni el scheduler se tocan.
"""

from __future__ import annotations

from memex.logging import get_logger
from memex.notifications.client import Notification, Notifier
from memex.notifications.persistent_notifier import PersistentNotifier

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
    """El `Notifier` activo: el servicio real que persiste los avisos en la cola `notifications`.

    Para volver al comportamiento solo-log (p.ej. en un entorno sin DB), devolver
    `LoggingNotifier()`.
    """
    return PersistentNotifier()
