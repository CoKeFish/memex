"""Contrato de notificaciones — el seam `Notifier` que cualquier emisor de avisos usa.

Por qué un Protocol y no una clase concreta: el código que EMITE avisos (hoy el daemon de
transporte) se tipa contra `Notifier`, nunca contra una implementación. Así los tests pasan un
notificador falso sin montar nada, y el servicio de notificaciones real (persistir + mostrar en el
dashboard) entra después SIN tocar a los emisores — solo cambia `build_notifier` (ver
`logging_notifier.py`). `LoggingNotifier` es UNA implementación válida (stub), no LA única.

El contrato es de USO GENERAL (no atado a geo/transporte): cualquier emisor manda su `kind`. La
persistencia real (cola + colapso por `dedup_key` + vista en el dashboard) vive en `store.py` y
`persistent_notifier.py`; `LoggingNotifier` queda como stub para tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class Notification:
    """Un aviso para el usuario, agnóstico del canal de entrega y del dominio que lo emite.

    `dedup_key` es la clave de idempotencia: un emisor puede re-emitir el MISMO aviso en cada
    corrida (p.ej. el daemon de transporte cada 10 min) y el `Notifier` real colapsa los repetidos
    por esa clave (por usuario). `payload` lleva las referencias estructuradas (ids, tiempos) para
    que la vista arme el detalle sin re-derivar nada. `deep_link` es a dónde navega la vista al
    abrir el aviso (lo decide el emisor, no un mapa por `kind` en el frontend). `expires_at` marca
    cuándo el aviso deja de aplicar: los vencidos se ocultan en lectura y son purgables — `None` =
    sin vencimiento.
    """

    kind: str  # taxonomía del aviso, p.ej. "transport.leave_by"
    severity: str  # "info" | "alta" | "critica" (mismo vocabulario que StatsAlert)
    title: str
    body: str
    dedup_key: str
    created_at: datetime  # aware; el instante con el que el emisor lo generó
    user_id: int  # dueño del aviso (multi-tenant); el Notifier persiste por usuario
    payload: dict[str, Any] = field(default_factory=dict)
    deep_link: str | None = None  # ruta del dashboard a abrir al hacer clic, p.ej. "/calendario"
    expires_at: datetime | None = None  # aware; None = sin vencimiento


@runtime_checkable
class Notifier(Protocol):
    """Cualquier cosa capaz de entregar una `Notification` (dashboard, push, agente, log…)."""

    async def notify(self, notification: Notification) -> None:
        """Entrega (o encola) el aviso. Debe ser idempotente por `notification.dedup_key`."""
        ...
