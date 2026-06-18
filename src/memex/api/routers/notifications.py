"""Lectura y gestión de la cola de notificaciones (`GET /notifications` + acciones).

Superficie de LECTURA del servicio de notificaciones; la escritura (encolar) la hace el
`PersistentNotifier` por el seam `Notifier`, no por HTTP. Alimenta la página `/notificaciones` y el
AlertBell del dashboard. Multi-tenant: todo se acota a `current_user_id`.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from memex.api.auth import current_user_id
from memex.api.schemas import NotificationList
from memex.db import connection
from memex.logging import get_logger
from memex.notifications import store

router = APIRouter(prefix="/notifications", tags=["notifications"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.notifications")


@router.get("", response_model=NotificationList)
async def list_notifications(
    user_id: UserID,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: int | None = Query(default=None, description="id < cursor (paginación newest-first)"),
) -> dict[str, Any]:
    """Cola activa de avisos del usuario (newest-first). Excluye descartados y vencidos; incluye el
    conteo de no-leídas para el badge de la campana. Paginación por cursor (`id < :cursor`)."""
    with connection() as conn:
        items = store.list_active(conn, user_id=user_id, limit=limit, cursor=cursor)
        unread = store.count_unread(conn, user_id=user_id)
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "unread": unread, "next_cursor": next_cursor}


@router.post("/read-all")
async def mark_all_notifications_read(user_id: UserID) -> dict[str, int]:
    """Marca leídos todos los avisos activos sin leer del usuario. Devuelve cuántos cambió."""
    with connection() as conn:
        updated = store.mark_all_read(conn, user_id=user_id)
    _log.info("notifications.read_all", user_id=user_id, updated=updated)
    return {"updated": updated}


@router.post("/{notification_id}/read")
async def mark_notification_read(notification_id: int, user_id: UserID) -> dict[str, bool]:
    """Marca un aviso como leído (sale del conteo de no-leídas). 404 si no es del usuario."""
    with connection() as conn:
        ok = store.mark_read(conn, notification_id=notification_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="notification not found")
    return {"ok": True}


@router.post("/{notification_id}/dismiss")
async def dismiss_notification(notification_id: int, user_id: UserID) -> dict[str, bool]:
    """Descarta un aviso (lo saca de la cola activa). 404 si no es del usuario."""
    with connection() as conn:
        ok = store.dismiss(conn, notification_id=notification_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="notification not found")
    return {"ok": True}
