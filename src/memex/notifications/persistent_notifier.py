"""`Notifier` real: PERSISTE el aviso en la cola `notifications` (colapsando repetidos).

Es la implementación que `build_notifier()` cablea por default. Cualquier emisor (hoy el daemon de
transporte, mañana otros) entrega su `Notification` por el seam y acá queda encolada para la vista
del dashboard / la campana.
"""

from __future__ import annotations

from memex.db import connection
from memex.logging import get_logger
from memex.notifications import store
from memex.notifications.client import Notification

_log = get_logger("memex.notifications")


class PersistentNotifier:
    """`Notifier` que encola la `Notification` en `notifications` (idempotente por dedup_key).

    Abre su propia conexión síncrona dentro del `notify` async — mismo patrón que los routers del
    API y `assess_next_arrival`. El emisor entrega 1 aviso por corrida, así que el bloqueo del
    event loop es trivial; no hace falta `asyncio.to_thread`.
    """

    async def notify(self, notification: Notification) -> None:
        with connection() as conn:
            notification_id = store.enqueue(conn, notification)
        _log.info(
            "notification.persisted",
            id=notification_id,
            kind=notification.kind,
            severity=notification.severity,
            dedup_key=notification.dedup_key,
            user_id=notification.user_id,
        )
