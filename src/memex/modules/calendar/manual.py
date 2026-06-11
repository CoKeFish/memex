"""CRUD manual de eventos del calendario — la lógica detrás de `memex calendario …`.

El agente (Hermes) crea/lee/corrige/borra eventos por CLI sin pasar por LLM ni proveedor:

- `add`: fila cruda `origin='manual'` con rank 100 (igual que los manuales del proveedor,
  decisión 7: lo que la persona pone a mano GANA la consolidación). Una serie (`--every/--until`)
  son N instancias que comparten un `recurring_event_id` local `memex:<uuid>` — espejo del modelo
  por-instancias del proveedor (Google `singleEvents`), así el colapso de conflictos por serie
  funciona igual para series locales.
- `update`: determinista, SIN LLM. Si el ganador del consolidado ya es manual → se edita la fila
  cruda in-place Y el consolidado en la misma tx (el guard de estabilidad de la consolidación no
  reescribe campos con membresía idéntica, así que el doble-write es obligatorio). Si el ganador
  es de otra fuente → se inserta un manual nuevo con par de dedup CONFIRMADO contra todos los
  miembros vivos: la consolidación los une y el manual gana por rank.
- `rm`: tombstone `deleted_source='user'` del consolidado — definitivo (la consolidación nunca lo
  resucita; el push lo propaga como delete de las copias que memex escribió en el proveedor).

Cada mutación cierra con `run_consolidation(user)` para que la capa consolidada refleje al toque.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.dedup import DedupRow
from memex.modules.calendar.module import _mark_dedup

_log = get_logger("memex.modules.calendar.manual")

#: Rank de un evento manual: gana la consolidación frente a extracción/proveedor (decisión 7).
MANUAL_PRIORITY_RANK = 100
#: Tope de instancias que puede generar una serie local.
MAX_SERIES_INSTANCES = 400
#: Prefijo de los ids de serie locales (nunca colisiona con los del proveedor).
SERIES_PREFIX = "memex:"


class ManualEventError(Exception):
    """Error de uso del CRUD manual: el mensaje va al agente tal cual (exit 1 en el CLI)."""


def _hhmm(t: time | None) -> str | None:
    return t.isoformat()[:5] if t is not None else None


# --- series (puro) ------------------------------------------------------------------ #


def series_instances(start: date, every: str, until: date) -> list[date]:
    """Fechas de una serie local, `start` y `until` inclusive. `daily` +1d, `weekly` +7d,
    `monthly` mismo día del mes (los meses SIN ese día —ej. 31 en febrero— se saltan). Pura."""
    if until < start:
        raise ManualEventError("--until debe ser igual o posterior a --date.")
    out: list[date] = []
    if every in ("daily", "weekly"):
        step = timedelta(days=1 if every == "daily" else 7)
        d = start
        while d <= until:
            out.append(d)
            d += step
    elif every == "monthly":
        y, m = start.year, start.month
        while date(y, m, 1) <= until:
            try:
                d = date(y, m, start.day)
            except ValueError:
                d = None  # mes sin ese día → se salta (no se corre al último día)
            if d is not None and start <= d <= until:
                out.append(d)
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    else:
        raise ManualEventError(f"cadencia desconocida: {every!r} (daily|weekly|monthly).")
    if len(out) > MAX_SERIES_INSTANCES:
        raise ManualEventError(
            f"la serie generaría {len(out)} instancias (máximo {MAX_SERIES_INSTANCES}): "
            "acortá --until."
        )
    return out


# --- helpers DB ---------------------------------------------------------------------- #


def _insert_manual(
    conn: Connection,
    user_id: int,
    *,
    title: str,
    starts_on: date,
    ends_on: date | None,
    start_time: time | None,
    end_time: time | None,
    location: str,
    description: str,
    protected: bool,
    series_id: str | None,
) -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_calendar_events
                  (user_id, source_inbox_ids, title, starts_on, ends_on, start_time, end_time,
                   location, description, origin, manual, priority_rank, protected,
                   recurring_event_id)
                VALUES
                  (:uid, ARRAY[]::bigint[], :title, :starts_on, :ends_on, :start_time, :end_time,
                   :location, :description, 'manual', TRUE, :rank, :protected, :series)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "title": title,
                "starts_on": starts_on,
                "ends_on": ends_on,
                "start_time": start_time,
                "end_time": end_time,
                "location": location,
                "description": description,
                "rank": MANUAL_PRIORITY_RANK,
                "protected": protected,
                "series": series_id,
            },
        ).scalar_one()
    )


def _consolidated_ids_of(event_ids: list[int]) -> list[int]:
    """Ids de consolidado (en el orden de `event_ids`, sin repetir) tras la consolidación."""
    if not event_ids:
        return []
    with connection() as conn:
        rows = conn.execute(
            text(
                "SELECT event_id, consolidated_id FROM mod_calendar_event_links "
                "WHERE event_id = ANY(:ids)"
            ),
            {"ids": event_ids},
        ).all()
    by_event = {int(e): int(c) for e, c in rows}
    out: list[int] = []
    for eid in event_ids:
        cid = by_event.get(eid)
        if cid is not None and cid not in out:
            out.append(cid)
    return out


def _live_consolidated(conn: Connection, user_id: int, cons_id: int) -> dict[str, Any]:
    row = (
        conn.execute(
            text(
                """
                SELECT id, title, starts_on, ends_on, start_time, end_time, location,
                       description, winner_event_id, deleted
                FROM mod_calendar_consolidated WHERE id = :cid AND user_id = :uid
                """
            ),
            {"cid": cons_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None or row["deleted"]:
        raise ManualEventError(f"No existe el evento {cons_id} (o fue borrado).")
    return dict(row)


def _series_of(conn: Connection, cons_id: int) -> str | None:
    """Id de serie del consolidado: el primer `recurring_event_id` no nulo de un miembro VIVO."""
    val = conn.execute(
        text(
            """
            SELECT e.recurring_event_id
            FROM mod_calendar_event_links l
            JOIN mod_calendar_events e ON e.id = l.event_id
            WHERE l.consolidated_id = :cid AND e.recurring_event_id IS NOT NULL
              AND e.provider_status IS DISTINCT FROM 'cancelled'
            ORDER BY e.id LIMIT 1
            """
        ),
        {"cid": cons_id},
    ).scalar()
    return str(val) if val is not None else None


def _resolve_targets(user_id: int, cons_id: int, *, series: bool) -> list[int]:
    """[cons_id], o con `series` todos los consolidados VIVOS de la serie del objetivo."""
    with connection() as conn:
        _live_consolidated(conn, user_id, cons_id)
        if not series:
            return [cons_id]
        serie = _series_of(conn, cons_id)
        if serie is None:
            raise ManualEventError(f"el evento {cons_id} no pertenece a una serie recurrente.")
        rows = conn.execute(
            text(
                """
                SELECT DISTINCT l.consolidated_id
                FROM mod_calendar_event_links l
                JOIN mod_calendar_events e ON e.id = l.event_id
                JOIN mod_calendar_consolidated c ON c.id = l.consolidated_id
                WHERE c.user_id = :uid AND NOT c.deleted AND e.recurring_event_id = :serie
                ORDER BY l.consolidated_id
                """
            ),
            {"uid": user_id, "serie": serie},
        ).all()
        return [int(r[0]) for r in rows]


# --- add ------------------------------------------------------------------------------ #


def add_event(
    user_id: int,
    *,
    title: str,
    starts_on: date,
    ends_on: date | None = None,
    start_time: time | None = None,
    end_time: time | None = None,
    location: str = "",
    description: str = "",
    protected: bool = False,
    every: str | None = None,
    until: date | None = None,
) -> dict[str, Any]:
    """Crea un evento manual (o una serie). Corre el dedup FASE 1 y consolida al cierre."""
    title = title.strip()
    if not title:
        raise ManualEventError("--title no puede estar vacío.")
    if (every is None) != (until is None):
        raise ManualEventError("--every y --until van juntos (serie) o ninguno (evento único).")
    if ends_on is not None and ends_on < starts_on:
        raise ManualEventError("--end-date debe ser igual o posterior a --date.")

    dates = series_instances(starts_on, every, until) if every and until else [starts_on]
    series_id = f"{SERIES_PREFIX}{uuid.uuid4()}" if every else None
    span = (ends_on - starts_on) if ends_on is not None else None

    event_ids: list[int] = []
    with connection() as conn:
        rows: list[DedupRow] = []
        for d in dates:
            instance_end = (d + span) if span is not None else None
            event_id = _insert_manual(
                conn,
                user_id,
                title=title,
                starts_on=d,
                ends_on=instance_end,
                start_time=start_time,
                end_time=end_time,
                location=location,
                description=description,
                protected=protected,
                series_id=series_id,
            )
            event_ids.append(event_id)
            rows.append(
                DedupRow(
                    event_id=event_id,
                    title=title,
                    location=location,
                    starts_on=d,
                    ends_on=instance_end,
                    start_time=start_time,
                    end_time=end_time,
                )
            )
        pairs = _mark_dedup(conn, user_id, rows)

    run_consolidation(user_id)
    _log.info(
        "calendar.manual.added",
        user_id=user_id,
        events=len(event_ids),
        series=series_id is not None,
        dedup_pairs=len(pairs),
    )
    return {
        "event_ids": event_ids,
        "consolidated_ids": _consolidated_ids_of(event_ids),
        "series_id": series_id,
        "title": title,
        "starts_on": starts_on.isoformat(),
        "instances": len(event_ids),
        "dedup_pairs": len(pairs),
    }


# --- list / show ---------------------------------------------------------------------- #


def list_events(
    user_id: int, *, since: date, until: date | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Próximos eventos de la capa consolidada, orden cronológico."""
    where = "c.user_id = :uid AND NOT c.deleted AND c.starts_on >= :since"
    params: dict[str, Any] = {"uid": user_id, "since": since, "limit": limit}
    if until is not None:
        where += " AND c.starts_on <= :until"
        params["until"] = until
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT c.id, c.title, c.starts_on, c.ends_on, c.start_time, c.end_time,
                           c.location,
                           (SELECT count(*) FROM mod_calendar_event_links l
                             WHERE l.consolidated_id = c.id) AS member_count,
                           EXISTS (
                               SELECT 1 FROM mod_calendar_event_links l
                               JOIN mod_calendar_events e ON e.id = l.event_id
                               WHERE l.consolidated_id = c.id
                                 AND e.recurring_event_id IS NOT NULL) AS recurring
                    FROM mod_calendar_consolidated c
                    WHERE {where}
                    ORDER BY c.starts_on, c.start_time NULLS FIRST, c.id
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
    return [
        {
            "id": int(r["id"]),
            "title": r["title"],
            "starts_on": r["starts_on"].isoformat(),
            "ends_on": r["ends_on"].isoformat() if r["ends_on"] is not None else None,
            "start_time": _hhmm(r["start_time"]),
            "end_time": _hhmm(r["end_time"]),
            "location": r["location"],
            "member_count": int(r["member_count"]),
            "recurring": bool(r["recurring"]),
        }
        for r in rows
    ]


def show_event(user_id: int, cons_id: int) -> dict[str, Any]:
    """Detalle de un consolidado: campos + miembros (origen/ganador) + serie + conflictos."""
    with connection() as conn:
        cons = _live_consolidated(conn, user_id, cons_id)
        members = (
            conn.execute(
                text(
                    """
                    SELECT e.id, e.origin, e.provider, e.manual, e.protected, e.priority_rank,
                           e.recurring_event_id, e.source_inbox_ids, e.provider_status,
                           (e.id = :winner) AS is_winner
                    FROM mod_calendar_event_links l
                    JOIN mod_calendar_events e ON e.id = l.event_id
                    WHERE l.consolidated_id = :cid
                    ORDER BY e.id
                    """
                ),
                {"cid": cons_id, "winner": cons["winner_event_id"]},
            )
            .mappings()
            .all()
        )
        conflicts = (
            conn.execute(
                text(
                    """
                    SELECT cf.id, co.id AS other_id, co.title AS other_title,
                           co.starts_on AS other_starts_on
                    FROM mod_calendar_conflicts cf
                    JOIN mod_calendar_consolidated co ON co.id = CASE
                        WHEN cf.consolidated_a_id = :cid THEN cf.consolidated_b_id
                        ELSE cf.consolidated_a_id END
                    WHERE cf.user_id = :uid AND cf.status = 'pending'
                      AND :cid IN (cf.consolidated_a_id, cf.consolidated_b_id)
                    ORDER BY cf.id
                    """
                ),
                {"cid": cons_id, "uid": user_id},
            )
            .mappings()
            .all()
        )
        serie = _series_of(conn, cons_id)
    return {
        "id": cons_id,
        "title": cons["title"],
        "starts_on": cons["starts_on"].isoformat(),
        "ends_on": cons["ends_on"].isoformat() if cons["ends_on"] is not None else None,
        "start_time": _hhmm(cons["start_time"]),
        "end_time": _hhmm(cons["end_time"]),
        "location": cons["location"],
        "description": cons["description"],
        "series_id": serie,
        "members": [
            {
                "event_id": int(m["id"]),
                "origin": m["origin"],
                "provider": m["provider"],
                "manual": bool(m["manual"]),
                "protected": bool(m["protected"]),
                "priority_rank": int(m["priority_rank"]),
                "series_id": m["recurring_event_id"],
                "source_inbox_ids": [int(x) for x in m["source_inbox_ids"]],
                "cancelled": m["provider_status"] == "cancelled",
                "is_winner": bool(m["is_winner"]),
            }
            for m in members
        ],
        "pending_conflicts": [
            {
                "conflict_id": int(c["id"]),
                "with_id": int(c["other_id"]),
                "with_title": c["other_title"],
                "with_starts_on": c["other_starts_on"].isoformat(),
            }
            for c in conflicts
        ],
    }


# --- update --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EventChanges:
    """Campos a corregir (None = sin cambio; no hay 'vaciar' en v1)."""

    title: str | None = None
    starts_on: date | None = None
    ends_on: date | None = None
    start_time: time | None = None
    end_time: time | None = None
    location: str | None = None
    description: str | None = None

    def empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in dataclass_fields(self))


def _confirm_pair(conn: Connection, user_id: int, a: int, b: int) -> None:
    """Par de dedup CONFIRMADO por decisión manual (la FASE 1 pudo dejarlo como candidate)."""
    lo, hi = (a, b) if a < b else (b, a)
    conn.execute(
        text(
            """
            INSERT INTO mod_calendar_dedup_candidates
              (user_id, event_a_id, event_b_id, reason, score, status, decided_by, rationale,
               decided_at)
            VALUES (:uid, :a, :b, 'manual_update', 1.000, 'confirmed', 'manual',
                    'actualización manual del usuario', NOW())
            ON CONFLICT (event_a_id, event_b_id) DO UPDATE SET
              status = 'confirmed', decided_by = 'manual', reason = 'manual_update',
              score = 1.000, rationale = 'actualización manual del usuario', decided_at = NOW()
            """
        ),
        {"uid": user_id, "a": lo, "b": hi},
    )


def _apply_update(
    conn: Connection, user_id: int, cons_id: int, changes: EventChanges
) -> dict[str, Any]:
    cons = _live_consolidated(conn, user_id, cons_id)
    new_title: str = changes.title if changes.title is not None else str(cons["title"])
    new_starts_on: date = changes.starts_on if changes.starts_on is not None else cons["starts_on"]
    new_ends_on: date | None = changes.ends_on if changes.ends_on is not None else cons["ends_on"]
    new_start_time: time | None = (
        changes.start_time if changes.start_time is not None else cons["start_time"]
    )
    new_end_time: time | None = (
        changes.end_time if changes.end_time is not None else cons["end_time"]
    )
    new_location: str = changes.location if changes.location is not None else str(cons["location"])
    new_description: str = (
        changes.description if changes.description is not None else str(cons["description"])
    )
    new: dict[str, Any] = {
        "title": new_title,
        "starts_on": new_starts_on,
        "ends_on": new_ends_on,
        "start_time": new_start_time,
        "end_time": new_end_time,
        "location": new_location,
        "description": new_description,
    }

    winner = None
    if cons["winner_event_id"] is not None:
        winner = (
            conn.execute(
                text(
                    "SELECT id, origin, protected, recurring_event_id FROM mod_calendar_events "
                    "WHERE id = :wid AND provider_status IS DISTINCT FROM 'cancelled'"
                ),
                {"wid": int(cons["winner_event_id"])},
            )
            .mappings()
            .first()
        )

    if winner is not None and winner["origin"] == "manual":
        # In-place: fila cruda + consolidado en la MISMA tx. El guard de estabilidad de la
        # consolidación no reescribe campos con membresía idéntica — sin el doble-write el
        # cambio no se vería en la capa consolidada.
        conn.execute(
            text(
                """
                UPDATE mod_calendar_events SET
                  title = :title, starts_on = :starts_on, ends_on = :ends_on,
                  start_time = :start_time, end_time = :end_time, location = :location,
                  description = :description
                WHERE id = :id
                """
            ),
            {**new, "id": int(winner["id"])},
        )
        conn.execute(
            text(
                """
                UPDATE mod_calendar_consolidated SET
                  title = :title, starts_on = :starts_on, ends_on = :ends_on,
                  start_time = :start_time, end_time = :end_time, location = :location,
                  description = :description, updated_at = NOW()
                WHERE id = :id
                """
            ),
            {**new, "id": cons_id},
        )
        return {"consolidated_id": cons_id, "mode": "in_place", "event_id": int(winner["id"])}

    # Override: manual nuevo (hereda serie y protección del ganador) + pares CONFIRMADOS contra
    # todos los miembros vivos → la consolidación une el grupo y el manual gana por rank 100.
    serie = _series_of(conn, cons_id)
    new_id = _insert_manual(
        conn,
        user_id,
        title=new_title,
        starts_on=new_starts_on,
        ends_on=new_ends_on,
        start_time=new_start_time,
        end_time=new_end_time,
        location=new_location,
        description=new_description,
        protected=bool(winner["protected"]) if winner is not None else False,
        series_id=serie,
    )
    _mark_dedup(
        conn,
        user_id,
        [
            DedupRow(
                event_id=new_id,
                title=new_title,
                location=new_location,
                starts_on=new_starts_on,
                ends_on=new_ends_on,
                start_time=new_start_time,
                end_time=new_end_time,
            )
        ],
    )
    member_ids = [
        int(r[0])
        for r in conn.execute(
            text(
                """
                SELECT e.id FROM mod_calendar_event_links l
                JOIN mod_calendar_events e ON e.id = l.event_id
                WHERE l.consolidated_id = :cid
                  AND e.provider_status IS DISTINCT FROM 'cancelled'
                """
            ),
            {"cid": cons_id},
        ).all()
    ]
    for mid in member_ids:
        _confirm_pair(conn, user_id, new_id, mid)
    return {"consolidated_id": cons_id, "mode": "manual_override", "event_id": new_id}


def update_event(
    user_id: int, cons_id: int, changes: EventChanges, *, series: bool = False
) -> dict[str, Any]:
    """Corrige un consolidado (o toda su serie). Determinista, sin LLM."""
    if changes.empty():
        raise ManualEventError("nada que actualizar: pasá al menos un campo a corregir.")
    if series and (changes.starts_on is not None or changes.ends_on is not None):
        raise ManualEventError(
            "--date/--end-date no se combinan con --series (cada instancia tiene su fecha)."
        )
    targets = _resolve_targets(user_id, cons_id, series=series)
    results: list[dict[str, Any]] = []
    with connection() as conn:
        for target in targets:
            results.append(_apply_update(conn, user_id, target, changes))
    run_consolidation(user_id)
    _log.info(
        "calendar.manual.updated", user_id=user_id, consolidated_id=cons_id, instances=len(results)
    )
    return {"updated": results, "instances": len(results)}


# --- rm ------------------------------------------------------------------------------- #


def remove_event(user_id: int, cons_id: int, *, series: bool = False) -> dict[str, Any]:
    """Tombstone `deleted_source='user'` del consolidado (o de toda su serie). Definitivo:
    la consolidación nunca lo resucita; el próximo push borra del proveedor SOLO las copias que
    memex escribió ahí (lo que el usuario creó directo en su Google no se toca)."""
    targets = _resolve_targets(user_id, cons_id, series=series)
    with connection() as conn:
        title = str(_live_consolidated(conn, user_id, cons_id)["title"])
        conn.execute(
            text(
                """
                UPDATE mod_calendar_consolidated
                SET deleted = TRUE, deleted_source = 'user', updated_at = NOW()
                WHERE id = ANY(:ids) AND user_id = :uid AND NOT deleted
                """
            ),
            {"ids": targets, "uid": user_id},
        )
    run_consolidation(user_id)
    _log.info(
        "calendar.manual.removed", user_id=user_id, consolidated_id=cons_id, instances=len(targets)
    )
    return {"removed": targets, "title": title, "instances": len(targets)}
