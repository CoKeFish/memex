"""Router de SOLO LECTURA del dominio `calendar` para el dashboard.

Expone la capa que la vista `/calendario` necesita (hoy era 100% mock): la capa consolidada
(`mod_calendar_consolidated` + sus miembros crudos vía `event_links`), los pares de dedup, los
conflictos pendientes, las corridas de sync y las cuentas de proveedor. Calca el patrón de
`finance.py`: `connection()` + SQL crudo + `.mappings()`, paginación por cursor, coerción
NUMERIC→float, scoping por `user_id`. Todo GET — la UI no muta nada en este slice.

NO expone secretos: de las cuentas de proveedor solo cruza `token_path_env` (el NOMBRE de la env
var, ADR-001) y un booleano `sync_token_present`, nunca el token/cursor.
"""

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    CalendarConflictList,
    CalendarDedupList,
    CalendarEventList,
    CalendarProviderAccountList,
    CalendarSyncRunList,
)
from memex.db import connection
from memex.logging import get_logger
from memex.modules.contract import normalize

router = APIRouter(prefix="/calendar", tags=["calendar"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.calendar")


@router.get("/events", response_model=CalendarEventList)
async def list_events(
    user_id: UserID,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    """Eventos consolidados del usuario (`mod_calendar_consolidated`) con sus miembros crudos.

    Es la capa que pintan el calendario mensual, la agenda y el inspector. Cada consolidado trae
    sus eventos crudos (`event_links` → `mod_calendar_events`), de donde se derivan `origins`,
    `member_count` y la prioridad/protección del ganador. El front trae todo paginando por cursor
    (igual que `/finance/transactions`) y filtra por mes en el cliente. Excluye los tombstone
    (`deleted`).
    """
    where: list[str] = ["user_id = :uid", "NOT deleted"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if cursor is not None:
        where.append("id > :cur")
        params["cur"] = cursor

    cons_sql = f"""
        SELECT id, title, starts_on, ends_on, start_time, end_time, location, description,
               winner_event_id
        FROM mod_calendar_consolidated
        WHERE {" AND ".join(where)}
        ORDER BY id
        LIMIT :limit
    """
    with connection() as conn:
        cons_rows = conn.execute(text(cons_sql), params).mappings().all()
        cons_ids = [int(r["id"]) for r in cons_rows]
        members_by_cons: dict[int, list[dict[str, Any]]] = {cid: [] for cid in cons_ids}
        if cons_ids:
            mem_rows = (
                conn.execute(
                    text(
                        """
                        SELECT l.consolidated_id, e.id, e.origin, e.provider, e.source_inbox_ids,
                               e.evidence, e.processing_outcome, e.protected, e.priority_rank
                        FROM mod_calendar_event_links l
                        JOIN mod_calendar_events e ON e.id = l.event_id
                        WHERE l.consolidated_id = ANY(:ids)
                        ORDER BY l.consolidated_id, e.id
                        """
                    ),
                    {"ids": cons_ids},
                )
                .mappings()
                .all()
            )
            for mr in mem_rows:
                members_by_cons[int(mr["consolidated_id"])].append(dict(mr))

    items: list[dict[str, Any]] = []
    for r in cons_rows:
        winner_id = r["winner_event_id"]
        members = members_by_cons[int(r["id"])]
        origins: list[str] = []
        for m in members:
            if m["origin"] not in origins:
                origins.append(str(m["origin"]))
        winner = next((m for m in members if m["id"] == winner_id), None)
        protected = bool(winner["protected"]) if winner else any(m["protected"] for m in members)
        priority_rank = (
            int(winner["priority_rank"])
            if winner
            else max((int(m["priority_rank"]) for m in members), default=0)
        )
        items.append(
            {
                "id": int(r["id"]),
                "title": r["title"],
                "starts_on": r["starts_on"],
                "ends_on": r["ends_on"],
                "start_time": r["start_time"],
                "end_time": r["end_time"],
                "location": r["location"],
                "description": r["description"],
                "member_count": len(members),
                "origins": origins,
                "protected": protected,
                "priority_rank": priority_rank,
                "members": [
                    {
                        "id": int(m["id"]),
                        "origin": m["origin"],
                        "provider": m["provider"],
                        "source_inbox_ids": list(m["source_inbox_ids"]),
                        "evidence": m["evidence"],
                        "processing_outcome": m["processing_outcome"],
                        "is_winner": m["id"] == winner_id,
                    }
                    for m in members
                ],
            }
        )
    next_cursor = items[-1]["id"] if len(items) == limit else None
    _log.info("calendar.events.listed", user_id=user_id, count=len(items))
    return {"items": items, "next_cursor": next_cursor}


def _lite(prefix: str, r: Any) -> dict[str, Any]:
    """Arma una `CalendarEventLiteRow` desde columnas con alias `{prefix}_*`."""
    return {
        "id": int(r[f"{prefix}_id"]),
        "title": r[f"{prefix}_title"],
        "starts_on": r[f"{prefix}_starts_on"],
        "start_time": r[f"{prefix}_start_time"],
        "location": r[f"{prefix}_location"],
        "origin": r[f"{prefix}_origin"],
        "provider": r[f"{prefix}_provider"],
        "source_inbox_ids": list(r[f"{prefix}_source_inbox_ids"]),
    }


@router.get("/dedup-candidates", response_model=CalendarDedupList)
async def list_dedup_candidates(
    user_id: UserID,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    """Pares de dedup (`mod_calendar_dedup_candidates`) con sus dos eventos crudos.

    El panel "descartados y fusiones" los muestra: `status` candidate/confirmed/rejected y, si la
    FASE 2 LLM (o una decisión manual) ya corrió, `decided_by`/`confidence`/`rationale`.
    """
    where: list[str] = ["d.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if status is not None:
        where.append("d.status = :status")
        params["status"] = status
    if cursor is not None:
        where.append("d.id > :cur")
        params["cur"] = cursor

    sql = f"""
        SELECT d.id, d.reason, d.score, d.status, d.decided_by, d.confidence, d.rationale,
               d.decided_at,
               a.id AS a_id, a.title AS a_title, a.starts_on AS a_starts_on,
               a.start_time AS a_start_time, a.location AS a_location, a.origin AS a_origin,
               a.provider AS a_provider, a.source_inbox_ids AS a_source_inbox_ids,
               b.id AS b_id, b.title AS b_title, b.starts_on AS b_starts_on,
               b.start_time AS b_start_time, b.location AS b_location, b.origin AS b_origin,
               b.provider AS b_provider, b.source_inbox_ids AS b_source_inbox_ids
        FROM mod_calendar_dedup_candidates d
        JOIN mod_calendar_events a ON a.id = d.event_a_id
        JOIN mod_calendar_events b ON b.id = d.event_b_id
        WHERE {" AND ".join(where)}
        ORDER BY d.id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    items: list[dict[str, Any]] = []
    for r in rows:
        items.append(
            {
                "id": int(r["id"]),
                "a": _lite("a", r),
                "b": _lite("b", r),
                "reason": r["reason"],
                "score": float(r["score"]) if r["score"] is not None else None,
                "status": r["status"],
                "decided_by": r["decided_by"],
                "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
                "rationale": r["rationale"],
                "decided_at": r["decided_at"],
            }
        )
    next_cursor = items[-1]["id"] if len(items) == limit else None
    _log.info("calendar.dedup.listed", user_id=user_id, count=len(items))
    return {"items": items, "next_cursor": next_cursor}


def _cons_lite(prefix: str, r: Any) -> dict[str, Any]:
    """Arma una `CalendarConsolidatedLiteRow` desde columnas con alias `{prefix}_*`.

    `priority_rank`/`protected` salen del evento ganador (LEFT JOIN → puede faltar si el ganador
    se borró; caemos a 0/false)."""
    return {
        "id": int(r[f"{prefix}_id"]),
        "title": r[f"{prefix}_title"],
        "starts_on": r[f"{prefix}_starts_on"],
        "ends_on": r[f"{prefix}_ends_on"],
        "start_time": r[f"{prefix}_start_time"],
        "end_time": r[f"{prefix}_end_time"],
        "location": r[f"{prefix}_location"],
        "priority_rank": int(r[f"{prefix}_rank"]) if r[f"{prefix}_rank"] is not None else 0,
        "protected": bool(r[f"{prefix}_protected"]),
    }


def _series_key(series: str | None, title: str, start_time: Any, cons_id: int) -> str:
    """Identidad de serie de un lado del conflicto, en orden de confianza:

    1. `recurring_event_id` de CUALQUIER miembro del consolidado (proveedor o serie local) — la
       señal autoritativa.
    2. Sin serie pero con título: (título normalizado, hora) — colapsa las instancias extraídas
       de correos ("Clase de cálculo" cada jueves 7:00) que no traen id de serie. La misma
       normalización del dedup (`normalize`).
    3. Sin nada: el consolidado es su propia "serie" única (`c<id>`), nunca agrupa.
    """
    if series:
        return series
    title_norm = normalize(title)
    if title_norm:
        return f"t:{title_norm}|{start_time or ''}"
    return f"c{cons_id}"


@router.get("/conflicts", response_model=CalendarConflictList)
async def list_conflicts(
    user_id: UserID,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
) -> dict[str, Any]:
    """Conflictos AGRUPADOS por par de series recurrentes.

    Dos consolidados distintos de alta importancia que chocan en horario. Las instancias de un
    mismo par de series se colapsan en UN item: `a`/`b` son el representante (la ocurrencia más
    próxima a hoy), `instance_count` cuántas veces se repite, `first_on`/`last_on` el rango. La
    serie de cada lado sale de CUALQUIER miembro del consolidado (no solo del ganador, que puede
    estar borrado o ser de extracción), con fallback por título+hora (`_series_key`). No pagina
    por cursor (los conflictos son pocos); `limit` es el tope de filas crudas a escanear.
    """
    where: list[str] = ["cf.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if status is not None:
        where.append("cf.status = :status")
        params["status"] = status

    sql = f"""
        SELECT cf.id, cf.reason, cf.status, cf.created_at,
               ca.id AS a_id, ca.title AS a_title, ca.starts_on AS a_starts_on,
               ca.ends_on AS a_ends_on, ca.start_time AS a_start_time, ca.end_time AS a_end_time,
               ca.location AS a_location, ea.priority_rank AS a_rank, ea.protected AS a_protected,
               (SELECT e2.recurring_event_id
                  FROM mod_calendar_event_links l2
                  JOIN mod_calendar_events e2 ON e2.id = l2.event_id
                 WHERE l2.consolidated_id = ca.id AND e2.recurring_event_id IS NOT NULL
                 ORDER BY e2.id LIMIT 1) AS a_series,
               cb.id AS b_id, cb.title AS b_title, cb.starts_on AS b_starts_on,
               cb.ends_on AS b_ends_on, cb.start_time AS b_start_time, cb.end_time AS b_end_time,
               cb.location AS b_location, eb.priority_rank AS b_rank, eb.protected AS b_protected,
               (SELECT e2.recurring_event_id
                  FROM mod_calendar_event_links l2
                  JOIN mod_calendar_events e2 ON e2.id = l2.event_id
                 WHERE l2.consolidated_id = cb.id AND e2.recurring_event_id IS NOT NULL
                 ORDER BY e2.id LIMIT 1) AS b_series
        FROM mod_calendar_conflicts cf
        JOIN mod_calendar_consolidated ca ON ca.id = cf.consolidated_a_id
        JOIN mod_calendar_consolidated cb ON cb.id = cf.consolidated_b_id
        LEFT JOIN mod_calendar_events ea ON ea.id = ca.winner_event_id
        LEFT JOIN mod_calendar_events eb ON eb.id = cb.winner_event_id
        WHERE {" AND ".join(where)}
        ORDER BY cf.id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    # Agrupar por (par-de-series no ordenado, status). El choque ocurre el mismo día → la fecha del
    # conflicto es `a_starts_on` (== `b_starts_on`).
    groups: dict[tuple[tuple[str, str], str], list[Any]] = {}
    for r in rows:
        a_key = _series_key(r["a_series"], str(r["a_title"]), r["a_start_time"], int(r["a_id"]))
        b_key = _series_key(r["b_series"], str(r["b_title"]), r["b_start_time"], int(r["b_id"]))
        pair = (a_key, b_key) if a_key <= b_key else (b_key, a_key)
        groups.setdefault((pair, str(r["status"])), []).append(r)

    today = date.today()
    items: list[dict[str, Any]] = []
    for grp in groups.values():
        dates = [r["a_starts_on"] for r in grp]
        upcoming = sorted(
            (r for r in grp if r["a_starts_on"] >= today), key=lambda r: r["a_starts_on"]
        )
        rep = upcoming[0] if upcoming else max(grp, key=lambda r: r["a_starts_on"])
        items.append(
            {
                "id": int(rep["id"]),
                "a": _cons_lite("a", rep),
                "b": _cons_lite("b", rep),
                "reason": rep["reason"],
                "status": rep["status"],
                "created_at": rep["created_at"],
                "instance_count": len(grp),
                "recurring": len(grp) > 1,
                "first_on": min(dates),
                "last_on": max(dates),
            }
        )
    items.sort(key=lambda it: (it["first_on"], it["id"]))
    _log.info("calendar.conflicts.listed", user_id=user_id, groups=len(items), raw=len(rows))
    return {"items": items, "next_cursor": None}


@router.get("/sync-runs", response_model=CalendarSyncRunList)
async def list_sync_runs(
    user_id: UserID,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: int | None = Query(default=None, description="id < cursor (orden descendente)"),
) -> dict[str, Any]:
    """Corridas de sync con el proveedor (`mod_calendar_sync_runs`), más recientes primero.

    `account` es la etiqueta legible `'<provider> · <label>'` resuelta por LEFT JOIN a la cuenta
    (la corrida la conserva aunque la cuenta se borre — FK ON DELETE SET NULL)."""
    where: list[str] = ["sr.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if cursor is not None:
        where.append("sr.id < :cur")
        params["cur"] = cursor

    sql = f"""
        SELECT sr.id, sr.direction, sr.pulled, sr.created, sr.modified, sr.deleted, sr.unchanged,
               sr.dedup_pairs, sr.errors, sr.status, sr.started_at, sr.finished_at,
               pa.provider, pa.account_label
        FROM mod_calendar_sync_runs sr
        LEFT JOIN mod_calendar_provider_accounts pa ON pa.id = sr.provider_account_id
        WHERE {" AND ".join(where)}
        ORDER BY sr.id DESC
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    items: list[dict[str, Any]] = []
    for r in rows:
        if r["provider"] is not None:
            account = f"{r['provider']} · {r['account_label']}"
        else:
            account = "cuenta eliminada"
        items.append(
            {
                "id": int(r["id"]),
                "account": account,
                "direction": r["direction"],
                "pulled": int(r["pulled"]),
                "created": int(r["created"]),
                "modified": int(r["modified"]),
                "deleted": int(r["deleted"]),
                "unchanged": int(r["unchanged"]),
                "dedup_pairs": int(r["dedup_pairs"]),
                "errors": int(r["errors"]),
                "status": r["status"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
            }
        )
    next_cursor = items[-1]["id"] if len(items) == limit else None
    _log.info("calendar.sync_runs.listed", user_id=user_id, count=len(items))
    return {"items": items, "next_cursor": next_cursor}


@router.get("/provider-accounts", response_model=CalendarProviderAccountList)
async def list_provider_accounts(user_id: UserID) -> dict[str, Any]:
    """Cuentas de proveedor de calendario (`mod_calendar_provider_accounts`).

    Sin secretos: `token_path_env` es el NOMBRE de la env var del token (ADR-001) y
    `sync_token_present` solo indica si hay cursor delta. El front deriva el `tokenState`.
    """
    sql = """
        SELECT id, provider, account_label, calendar_id, last_sync_at, token_path_env,
               enabled, write_back, (sync_token IS NOT NULL) AS sync_token_present
        FROM mod_calendar_provider_accounts
        WHERE user_id = :uid
        ORDER BY id
    """
    with connection() as conn:
        rows = conn.execute(text(sql), {"uid": user_id}).mappings().all()
    items = [dict(r) for r in rows]
    _log.info("calendar.provider_accounts.listed", user_id=user_id, count=len(items))
    return {"items": items}
