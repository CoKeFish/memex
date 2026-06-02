"""Endpoints del backfill segmentado (importación masiva por ventanas, montados bajo /sources).

Cada acción es síncrona y supervisada por el usuario (sin daemon): configurar el rango, avanzar una
ventana (o el resto), consultar el estado para restaurar la UI, y resetear. Solo se ofrece para
fuentes que ventanan por fecha (`supports_date_window`, hoy imap). La orquestación vive en
`memex.backfill.service`; este router solo valida ownership + elegibilidad y traduce a HTTP.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import Connection, text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    BackfillAdvanceOverride,
    BackfillAdvanceResponse,
    BackfillConfig,
    BackfillState,
)
from memex.backfill import service
from memex.db import connection
from memex.logging import get_logger
from memex.sources import supports_date_window

router = APIRouter(prefix="/sources", tags=["backfill"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.backfill")

_NOT_ELIGIBLE = (
    "La fuente de tipo {t!r} no admite ventanas de fecha; el backfill segmentado solo está "
    "disponible para correo (imap)."
)
_NO_JOB = "no hay backfill configurado para esta fuente"


def _owned_source_type(conn: Connection, user_id: int, source_id: int) -> str:
    row = (
        conn.execute(text("SELECT user_id, type FROM sources WHERE id = :sid"), {"sid": source_id})
        .mappings()
        .first()
    )
    if row is None or row["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="source not found")
    return str(row["type"])


def _assert_eligible(source_type: str) -> None:
    if not supports_date_window(source_type):
        raise HTTPException(status_code=422, detail=_NOT_ELIGIBLE.format(t=source_type))


@router.post("/{source_id}/backfill", response_model=BackfillState)
async def configure_backfill(
    source_id: int, body: BackfillConfig, user_id: UserID
) -> BackfillState:
    """Crea o reconfigura el backfill de la fuente (resetea la frontera al inicio del rango)."""
    with connection() as conn:
        _assert_eligible(_owned_source_type(conn, user_id, source_id))
        job = service.upsert_job(
            conn,
            user_id,
            source_id,
            range_start=body.range_start,
            range_end_inclusive=body.range_end,
            window_unit=body.window_unit,
            window_count=body.window_count,
            per_window_limit=body.per_window_limit,
        )
    _log.info(
        "backfill.configured",
        user_id=user_id,
        source_id=source_id,
        range_start=body.range_start.isoformat(),
        range_end=body.range_end.isoformat(),
        window=f"{body.window_count} {body.window_unit}",
    )
    return service.to_state(job)


@router.get("/{source_id}/backfill", response_model=BackfillState)
async def get_backfill(source_id: int, user_id: UserID) -> BackfillState:
    """Estado del backfill para restaurar la UI; 404 si la fuente no tiene uno configurado."""
    with connection() as conn:
        _owned_source_type(conn, user_id, source_id)
        job = service.get_job(conn, user_id, source_id)
    if job is None:
        raise HTTPException(status_code=404, detail=_NO_JOB)
    return service.to_state(job)


@router.post("/{source_id}/backfill/advance", response_model=BackfillAdvanceResponse)
async def advance_backfill(
    source_id: int,
    user_id: UserID,
    dry_run: Annotated[bool, Query()] = False,
    body: BackfillAdvanceOverride | None = None,
) -> BackfillAdvanceResponse:
    """Procesa la próxima ventana (mueve la frontera salvo en dry-run)."""
    with connection() as conn:
        _assert_eligible(_owned_source_type(conn, user_id, source_id))
    ov = body or BackfillAdvanceOverride()
    window, job = await service.advance_one(
        user_id,
        source_id,
        dry_run=dry_run,
        unit_override=ov.window_unit,
        count_override=ov.window_count,
    )
    return BackfillAdvanceResponse(window=window, state=service.to_state(job), dry_run=dry_run)


@router.post("/{source_id}/backfill/advance-rest", response_model=BackfillAdvanceResponse)
async def advance_backfill_rest(
    source_id: int, user_id: UserID, dry_run: Annotated[bool, Query()] = False
) -> BackfillAdvanceResponse:
    """Procesa todo lo que queda hasta el fin del rango en una sola ventana."""
    with connection() as conn:
        _assert_eligible(_owned_source_type(conn, user_id, source_id))
    window, job = await service.advance_rest(user_id, source_id, dry_run=dry_run)
    return BackfillAdvanceResponse(window=window, state=service.to_state(job), dry_run=dry_run)


@router.delete("/{source_id}/backfill", status_code=204)
async def delete_backfill(source_id: int, user_id: UserID) -> None:
    """Borra el backfill de la fuente (reset)."""
    with connection() as conn:
        _owned_source_type(conn, user_id, source_id)
        service.delete_job(conn, user_id, source_id)
    _log.info("backfill.deleted", user_id=user_id, source_id=source_id)
