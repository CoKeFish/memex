"""API del módulo bienestar: lectura + gestión de hábitos.

Los REGISTROS son de solo lectura (la escritura va por la CLI / el agente); los HÁBITOS, en cambio,
los gestiona el usuario desde el dashboard (alta/baja). Expone registros, resumen, actividad diaria,
hábitos con adherencia, y `POST`/`DELETE` de hábitos. El `tz` (IANA, default `America/Bogota`) fija
la TZ del bucket para que los períodos coincidan con el reloj de pared del usuario (mismo patrón que
`routers/metrics.py`).
"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    BienestarDaily,
    BienestarHabitCreate,
    BienestarHabitList,
    BienestarHabitRow,
    BienestarRegistroList,
    BienestarSummary,
)
from memex.db import connection
from memex.logging import get_logger
from memex.modules.bienestar import habits as habits_mod
from memex.modules.bienestar.module import list_registros, summary

router = APIRouter(prefix="/bienestar", tags=["bienestar"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.bienestar")

#: TZ por defecto del bucket (cuando el cliente no manda `tz`). Default a Bogota (TZ del usuario);
#: no asumir México; alineado con `routers/metrics.py`/`logs.py`.
_BUCKET_TZ = "America/Bogota"


def _resolve_tz(tz: str | None) -> str:
    """None → `_BUCKET_TZ`; nombre IANA inválido → 422 (el valor va como bind en `AT TIME ZONE`)."""
    if tz is None:
        return _BUCKET_TZ
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"timezone inválida: {tz}") from exc
    return tz


@router.get("/registros", response_model=BienestarRegistroList)
async def list_registros_endpoint(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
    category: str | None = None,
    activity: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """Registros del user (más nuevos primero), filtrables por período / categoría / actividad."""
    with connection() as conn:
        rows = list_registros(
            conn,
            user_id,
            since=since,
            until=until,
            category=category,
            activity=activity,
            limit=limit,
        )
    _log.info("bienestar.registros.listed", user_id=user_id, count=len(rows))
    return {"items": rows}


@router.get("/summary", response_model=BienestarSummary)
async def summary_endpoint(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
    days: int | None = None,
) -> dict[str, Any]:
    """Total + conteos por categoría y actividad del período (`days` = atajo: últimos N días)."""
    if days is not None and since is None:
        since = datetime.now(UTC) - timedelta(days=days)
    with connection() as conn:
        return summary(conn, user_id, since=since, until=until)


@router.get("/daily", response_model=BienestarDaily)
async def daily_endpoint(
    user_id: UserID,
    since: datetime | None = None,
    until: datetime | None = None,
    days: int | None = None,
    tz: str | None = None,
) -> dict[str, Any]:
    """Conteo de registros por día (en `tz`) y categoría — para el gráfico de actividad."""
    zone = _resolve_tz(tz)
    if days is not None and since is None:
        since = datetime.now(UTC) - timedelta(days=days)
    where = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "tz": zone}
    if since is not None:
        where.append("occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("occurred_at < :until")
        params["until"] = until
    sql = f"""
        SELECT (occurred_at AT TIME ZONE :tz)::date AS day, category, count(*) AS n
        FROM mod_bienestar_registros
        WHERE {" AND ".join(where)}
        GROUP BY day, category
        ORDER BY day
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    by_day: dict[str, dict[str, Any]] = {}
    for r in rows:
        day = r["day"].isoformat()
        entry = by_day.setdefault(day, {"day": day, "total": 0, "by_category": {}})
        entry["by_category"][str(r["category"])] = int(r["n"])
        entry["total"] += int(r["n"])
    return {"days": [by_day[d] for d in sorted(by_day)]}


@router.get("/habits", response_model=BienestarHabitList)
async def habits_endpoint(
    user_id: UserID,
    tz: str | None = None,
    periods: Annotated[int, Query(ge=1, le=60)] = 14,
) -> dict[str, Any]:
    """Hábitos activos con adherencia: progreso del período, racha e historia (en `tz`)."""
    zone = _resolve_tz(tz)
    with connection() as conn:
        items = habits_mod.adherence(conn, user_id, tz=zone, periods=periods)
    return {"items": items}


@router.post("/habits", response_model=BienestarHabitRow, status_code=201)
async def create_habit_endpoint(user_id: UserID, body: BienestarHabitCreate) -> dict[str, Any]:
    """Crea un hábito (lo usa el dashboard). Necesita `activity` o `category`; el dominio valida y
    responde 422 si falta o la cadencia es inválida."""
    try:
        with connection() as conn:
            row = habits_mod.add_habit(
                conn,
                user_id,
                name=body.name,
                cadence=body.cadence,
                target_count=body.target_count,
                activity=body.activity,
                category=body.category,
            )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    _log.info("bienestar.habit.created", user_id=user_id, habit_id=row["id"], name=body.name)
    return row


@router.delete("/habits/{habit_id}")
async def delete_habit_endpoint(habit_id: int, user_id: UserID) -> dict[str, bool]:
    """Borra un hábito del user. 404 si no existe."""
    with connection() as conn:
        ok = habits_mod.delete_habit(conn, user_id, habit_id)
    if not ok:
        raise HTTPException(status_code=404, detail="hábito no encontrado")
    _log.info("bienestar.habit.deleted", user_id=user_id, habit_id=habit_id)
    return {"deleted": True}
