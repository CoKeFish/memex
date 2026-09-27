"""API del módulo tareas: lectura con los mismos filtros del CLI (estado —pendiente por default—,
vencidas, fecha límite, asunto, texto) y el cambio de estado desde el dashboard (completar, reabrir,
cancelar). El alta y la edición siguen por la CLI / el agente."""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from memex.api.auth import current_user_id
from memex.api.schemas import (
    TareaDiscard,
    TareaList,
    TareaMerge,
    TareaParentSet,
    TareaRow,
    TareaStatusSet,
)
from memex.db import connection
from memex.modules.tareas.module import (
    TareaNotFoundError,
    discard,
    get,
    list_tareas,
    set_status,
    update,
)

router = APIRouter(prefix="/tareas", tags=["tareas"])

UserID = Annotated[int, Depends(current_user_id)]


@router.get("", response_model=TareaList)
async def list_tareas_endpoint(
    user_id: UserID,
    status: str = "pendiente",
    overdue: bool = False,
    due_before: date | None = None,
    asunto_id: int | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    category: str | None = None,
) -> dict[str, Any]:
    """Tareas del user: seguridad primero, luego vencidas, fecha límite, las sin fecha al final.
    `category` filtra (seguridad | entrega | tramite | pago | inscripcion | dinamica | otro)."""
    try:
        with connection() as conn:
            rows = list_tareas(
                conn,
                user_id,
                status=status,
                overdue_only=overdue,
                due_before=due_before,
                asunto_id=asunto_id,
                q=q,
                limit=limit,
                category=category,
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    return {"count": len(rows), "items": rows}


@router.get("/{tarea_id}", response_model=TareaRow)
async def get_tarea_endpoint(user_id: UserID, tarea_id: int) -> dict[str, Any]:
    try:
        with connection() as conn:
            return get(conn, user_id, tarea_id)
    except TareaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{tarea_id}/discard", response_model=TareaRow)
async def discard_endpoint(user_id: UserID, tarea_id: int, body: TareaDiscard) -> dict[str, Any]:
    """«No es una tarea»: la cancela, bloquea que la extracción la vuelva a crear y deja feedback
    sobre el mensaje de origen."""
    try:
        with connection() as conn:
            return discard(conn, user_id, tarea_id, reason=body.reason, note=body.note)
    except TareaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{tarea_id}/parent", response_model=TareaRow)
async def set_parent_endpoint(
    user_id: UserID, tarea_id: int, body: TareaParentSet
) -> dict[str, Any]:
    """Cuelga la tarea como paso de un proyecto (otra tarea del mismo asunto) o la descuelga."""
    try:
        with connection() as conn:
            return update(conn, user_id, tarea_id, parent=body.parent_id)
    except TareaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/{tarea_id}/merge", response_model=TareaRow)
async def merge_endpoint(user_id: UserID, tarea_id: int, body: TareaMerge) -> dict[str, Any]:
    """«Estas dos son la misma»: la tarea `absorb_id` se funde en `tarea_id` (que es la que
    queda) y queda cancelada. Devuelve la que queda, ya enriquecida."""
    from memex.modules.tareas.module import get, merge_tareas

    try:
        with connection() as conn:
            merge_tareas(conn, user_id, into=tarea_id, absorb=body.absorb_id)
            return get(conn, user_id, tarea_id)
    except TareaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/{tarea_id}/status", response_model=TareaRow)
async def set_status_endpoint(
    user_id: UserID, tarea_id: int, body: TareaStatusSet
) -> dict[str, Any]:
    """Completar (`completada`), reabrir (`pendiente`) o cancelar una tarea desde el dashboard."""
    try:
        with connection() as conn:
            return set_status(conn, user_id, tarea_id, body.status)
    except TareaNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
