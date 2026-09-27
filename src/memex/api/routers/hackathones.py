from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import HackathonList
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/hackathones", tags=["hackathones"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.hackathones")


@router.get("/events", response_model=HackathonList)
async def list_hackathones(
    user_id: UserID,
    modality: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    """Lista los hackatones extraídos por el módulo hackathones (tabla `mod_hackathones_events`).

    Devuelve filas crudas; el dashboard agrega/ordena en el cliente. La paginación es por cursor
    (`id > :cur`) igual que `/inbox` y finance. Los filtros `modality`/`since`/`until`
    (sobre `starts_on`) espejan el patrón de finance; las filas sin fecha quedan fuera al filtrar
    por rango (es el comportamiento esperado: filtrar por fecha pide tener fecha).
    """
    where: list[str] = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if modality is not None:
        where.append("modality = :modality")
        params["modality"] = modality
    if since is not None:
        where.append("starts_on >= :since")
        params["since"] = since
    if until is not None:
        where.append("starts_on < :until")
        params["until"] = until
    if cursor is not None:
        where.append("id > :cur")
        params["cur"] = cursor

    sql = f"""
        SELECT id, name, starts_on, ends_on, registration_deadline, modality, location, url,
               organizer, technologies, prizes, requirements, description, evidence,
               source_inbox_ids, created_at
        FROM mod_hackathones_events
        WHERE {" AND ".join(where)}
        ORDER BY id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    items = [dict(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    _log.info("hackathones.events.listed", user_id=user_id, count=len(items), modality=modality)
    return {"items": items, "next_cursor": next_cursor}
