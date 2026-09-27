"""Salud de una cuenta de WhatsApp para «Validar» en /cuenta y el auto-chequeo del daemon.

No hay credenciales que probar: la cuenta es un dispositivo vinculado. La evidencia es el estado del
vínculo (`whatsapp_links.status`) y la conexión: la viva de ESTE proceso si el cliente vive acá (el
API), o la que dejó persistida el runtime (`vinculado` = conectado; al caerse escribe
`desconectado`) cuando el chequeo corre en otro proceso (daemon de notificaciones, consola).
Antes, desde el daemon siempre daba «degraded»: falsa alarma.
"""

from __future__ import annotations

from datetime import UTC, datetime

from memex.core.source import HealthResult
from memex.db import connection
from memex.whatsapp import store
from memex.whatsapp.runtime import runtime

#: `HealthResult.code`: no hay vínculo vigente; hay que pedir un código en la tarjeta de WhatsApp.
HEALTH_CODE_LINK_REQUIRED = "link_required"


def assess(status: str, connected: bool, last_error: str | None = None) -> HealthResult:
    """Estado del vínculo + conexión viva → salud. Puro (testeable sin DB ni cliente)."""
    now = datetime.now(UTC)
    if status == "vinculado":
        if connected:
            return HealthResult(
                status="healthy", detail="dispositivo vinculado y conectado", checked_at=now
            )
        return HealthResult(
            status="degraded",
            detail="vinculado pero sin conexión en este momento (reconecta solo)",
            checked_at=now,
        )
    if status == "desconectado":
        return HealthResult(
            status="degraded", detail="conexión caída; reconecta solo", checked_at=now
        )
    if status == "codigo":
        return HealthResult(
            status="unhealthy",
            detail="esperando que escribas el código en el teléfono",
            checked_at=now,
            code=HEALTH_CODE_LINK_REQUIRED,
        )
    if status == "baneado":
        tail = f" ({last_error})" if last_error else ""
        return HealthResult(
            status="unhealthy", detail=f"WhatsApp suspendió el dispositivo{tail}", checked_at=now
        )
    return HealthResult(
        status="unhealthy",
        detail="sin vincular: pedí un código en la tarjeta de WhatsApp",
        checked_at=now,
        code=HEALTH_CODE_LINK_REQUIRED,
    )


async def health_check(account_id: int) -> HealthResult:
    with connection() as conn:
        link = store.get_link(conn, account_id)
    status = str(link["status"]) if link else "sin_vincular"
    last_error = str(link["last_error"]) if link and link.get("last_error") else None
    connected = (
        runtime.is_connected(account_id) if runtime.owns(account_id) else status == "vinculado"
    )
    return assess(status, connected, last_error)
