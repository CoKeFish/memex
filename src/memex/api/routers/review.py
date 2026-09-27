"""Cola de revisión (/revision): dead-letter REAL + acción de reencolar.

Reemplaza el mock de la vista. El dead-letter (mensajes que cruzaron MAX_WORK_ATTEMPTS fallos en
summarize/extract y quedaron 'pendientes de revisión', ver `core/deadletter`) no tenía superficie
HTTP; acá se lista con contexto del inbox y se expone "reencolar" (lo saca de revisión → vuelve al
work-set para reintentarse). Los conflictos de calendario se leen por su endpoint existente
(`/calendar/conflicts`); su resolución es un slice del módulo calendar, no de esta vista.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import ReviewActionResult, ReviewDeadLetterItem
from memex.core.deadletter import STAGE_EXTRACT, STAGE_SUMMARIZE, requeue
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/review", tags=["review"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.review")

_STAGES = frozenset({STAGE_SUMMARIZE, STAGE_EXTRACT})
#: Campos del payload con los que se arma un preview legible (cubre correo y chat).
_PREVIEW_FIELDS = ("subject", "body_text", "text", "caption")


def _preview(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts = [str(payload[k]).strip() for k in _PREVIEW_FIELDS if payload.get(k)]
    return " · ".join(p for p in parts if p)[:400]


@router.get("/dead-letter", response_model=list[ReviewDeadLetterItem])
async def list_dead_letter(user_id: UserID) -> list[dict[str, Any]]:
    """Dead-letter de summarize/extract ('pendiente de revisión') con contexto del inbox, más
    recientes primero. Lista vacía = nada pendiente."""
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT wf.id, wf.stage, wf.inbox_id, wf.attempts, wf.last_error,
                           wf.status, wf.created_at, wf.updated_at, i.payload
                    FROM work_item_failures wf
                    JOIN inbox i ON i.id = wf.inbox_id
                    WHERE wf.user_id = :uid AND wf.status = 'review'
                    ORDER BY wf.updated_at DESC
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    return [
        {
            "id": int(r["id"]),
            "stage": str(r["stage"]),
            "inbox_id": int(r["inbox_id"]),
            "attempts": int(r["attempts"]),
            "last_error": r["last_error"],
            "status": str(r["status"]),
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "preview": _preview(r["payload"]),
        }
        for r in rows
    ]


@router.post("/dead-letter/{stage}/{inbox_id}/requeue", response_model=ReviewActionResult)
async def requeue_dead_letter(stage: str, inbox_id: int, user_id: UserID) -> dict[str, Any]:
    """Reencola: saca el mensaje de revisión → vuelve al work-set (se reintenta luego)."""
    if stage not in _STAGES:
        raise HTTPException(status_code=422, detail=f"stage inválida: {stage!r}")
    if not requeue(user_id, stage, inbox_id):
        raise HTTPException(status_code=404, detail="el mensaje no estaba en revisión")
    _log.info("review.requeue", user_id=user_id, stage=stage, inbox_id=inbox_id)
    return {"ok": True}
