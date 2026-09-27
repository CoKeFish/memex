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
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import date, time, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.geo import places as geo_places
from memex.logging import get_logger
from memex.modules.calendar.commitment import LEVELS, normalize_level
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.dedup import DedupRow
from memex.modules.calendar.module import _mark_dedup
from memex.modules.calendar.recurrence import (
    RecurrenceError,
    RecurrenceSpec,
    describe,
    exdate_line,
    expand_rows,
    is_recurring,
    next_occurrences,
    normalize_day,
    normalize_freq,
    occurs_on,
    split_rrule,
    to_rrule,
    validate_lines,
)

_log = get_logger("memex.modules.calendar.manual")

#: Rank de un evento manual: gana la consolidación frente a extracción/proveedor (decisión 7).
MANUAL_PRIORITY_RANK = 100
#: Tope de instancias que puede generar una serie local.
MAX_SERIES_INSTANCES = 400
#: Prefijo de los ids de serie locales (nunca colisiona con los del proveedor).
from memex.modules.calendar.recurrence import SERIES_PREFIX  # noqa: E402


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
    recurrence: Sequence[str] = (),
    original_start_on: date | None = None,
    original_start_time: time | None = None,
) -> int:
    return int(
        conn.execute(
            text(
                """
                INSERT INTO mod_calendar_events
                  (user_id, source_inbox_ids, title, starts_on, ends_on, start_time, end_time,
                   location, description, origin, manual, priority_rank, protected,
                   recurring_event_id, recurrence, original_start_on, original_start_time,
                   commitment, commitment_source)
                VALUES
                  (:uid, ARRAY[]::bigint[], :title, :starts_on, :ends_on, :start_time, :end_time,
                   :location, :description, 'manual', TRUE, :rank, :protected, :series,
                   :recurrence, :orig_on, :orig_time, 'confirmado', 'manual')
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
                "recurrence": list(recurrence),
                "orig_on": original_start_on,
                "orig_time": original_start_time,
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
                       description, winner_event_id, deleted, place_id, recurrence, series_id,
                       original_start_on, original_start_time, commitment, commitment_evidence
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
    byday: Sequence[str] = (),
    interval: int = 1,
    count: int | None = None,
) -> dict[str, Any]:
    """Crea un evento manual. Con `every` crea UN MAESTRO recurrente (estilo Google: una fila con
    RRULE, ocurrencias expandidas al leer; `until`/`count` opcionales = sin fin). Corre el dedup
    FASE 1 y consolida al cierre."""
    title = title.strip()
    if not title:
        raise ManualEventError("--title no puede estar vacío.")
    if every is None and (until is not None or byday or count is not None):
        raise ManualEventError("--until/--byday/--count solo tienen sentido con --every.")
    if ends_on is not None and ends_on < starts_on:
        raise ManualEventError("--end-date debe ser igual o posterior a --date.")
    if until is not None and until < starts_on:
        raise ManualEventError("--until debe ser igual o posterior a --date.")

    recurrence: tuple[str, ...] = ()
    if every is not None:
        try:
            spec = RecurrenceSpec(
                freq=normalize_freq(every),
                interval=interval,
                byday=tuple(normalize_day(d) for d in byday),
                until=until,
                count=count,
            )
        except RecurrenceError as e:
            raise ManualEventError(str(e)) from e
        recurrence = (to_rrule(spec),)
    series_id = f"{SERIES_PREFIX}{uuid.uuid4()}" if recurrence else None

    with connection() as conn:
        event_id = _insert_manual(
            conn,
            user_id,
            title=title,
            starts_on=starts_on,
            ends_on=ends_on,
            start_time=start_time,
            end_time=end_time,
            location=location,
            description=description,
            protected=protected,
            series_id=series_id,
            recurrence=recurrence,
        )
        pairs = _mark_dedup(
            conn,
            user_id,
            [
                DedupRow(
                    event_id=event_id,
                    title=title,
                    location=location,
                    starts_on=starts_on,
                    ends_on=ends_on,
                    start_time=start_time,
                    end_time=end_time,
                    recurrence=recurrence,
                )
            ],
        )

    run_consolidation(user_id)
    _log.info(
        "calendar.manual.added",
        user_id=user_id,
        event_id=event_id,
        recurring=bool(recurrence),
        dedup_pairs=len(pairs),
    )
    return {
        "event_ids": [event_id],
        "consolidated_ids": _consolidated_ids_of([event_id]),
        "series_id": series_id,
        "recurrence": list(recurrence),
        "recurrence_human": describe(recurrence),
        "title": title,
        "starts_on": starts_on.isoformat(),
        "instances": 1,
        "dedup_pairs": len(pairs),
    }


# --- list / show ---------------------------------------------------------------------- #


#: Sin `--until`, hasta dónde se expanden los maestros recurrentes («próximos eventos»): una regla
#: sin fin no puede listarse entera, y con `limit` lo que importa es lo cercano.
LIST_HORIZON = timedelta(days=400)


def list_events(
    user_id: int, *, since: date, until: date | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    """Eventos de la capa consolidada que caen en `[since, until]`, cronológicos. Los maestros
    recurrentes vienen EXPANDIDOS: una fila por ocurrencia (`starts_on` = el día; `series_id` y
    `recurrence` identifican la serie), salvo las ocurrencias que una excepción reemplaza; las
    excepciones entran por su propia fecha. `until` default = `since` + 400 días."""
    window_end = until if until is not None else since + LIST_HORIZON
    params: dict[str, Any] = {"uid": user_id, "since": since, "until": window_end}
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT c.id, c.title, c.starts_on, c.ends_on, c.start_time, c.end_time,
                           c.location, c.recurrence, c.series_id, c.original_start_on,
                           c.commitment,
                           (SELECT count(*) FROM mod_calendar_event_links l
                             WHERE l.consolidated_id = c.id) AS member_count
                    FROM mod_calendar_consolidated c
                    WHERE c.user_id = :uid AND NOT c.deleted
                      AND (COALESCE(c.ends_on, c.starts_on) >= :since AND c.starts_on <= :until
                           OR cardinality(c.recurrence) > 0
                           OR c.original_start_on BETWEEN :since AND :until)
                    ORDER BY c.starts_on, c.start_time NULLS FIRST, c.id
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )
    expanded = expand_rows([dict(r) for r in rows], window_start=since, window_end=window_end)
    expanded.sort(key=lambda rd: (rd[1], rd[0]["start_time"] or time.min, int(rd[0]["id"])))
    out: list[dict[str, Any]] = []
    for r, day in expanded[:limit]:
        rec = tuple(r["recurrence"] or ())
        recurring = is_recurring(rec)
        out.append(
            {
                "id": int(r["id"]),
                "title": r["title"],
                "starts_on": day.isoformat(),
                "ends_on": (
                    None
                    if recurring
                    else (r["ends_on"].isoformat() if r["ends_on"] is not None else None)
                ),
                "start_time": _hhmm(r["start_time"]),
                "end_time": _hhmm(r["end_time"]),
                "location": r["location"],
                "member_count": int(r["member_count"]),
                "recurring": recurring or r["series_id"] is not None,
                "series_id": r["series_id"],
                "recurrence": list(rec),
                "recurrence_human": describe(rec) if recurring else "",
                "is_exception": r["original_start_on"] is not None,
                "commitment": r["commitment"],
            }
        )
    return out


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
        place = (
            geo_places.get_place(conn, user_id, int(cons["place_id"]))
            if cons["place_id"] is not None
            else None
        )
    rec = tuple(cons["recurrence"] or ())
    upcoming = (
        next_occurrences(cons["starts_on"], rec, after=date.today(), start_time=cons["start_time"])
        if is_recurring(rec)
        else []
    )
    return {
        "id": cons_id,
        "title": cons["title"],
        "starts_on": cons["starts_on"].isoformat(),
        "ends_on": cons["ends_on"].isoformat() if cons["ends_on"] is not None else None,
        "start_time": _hhmm(cons["start_time"]),
        "end_time": _hhmm(cons["end_time"]),
        "location": cons["location"],
        "description": cons["description"],
        "series_id": cons["series_id"] or serie,
        "recurrence": list(rec),
        "recurrence_human": describe(rec) if is_recurring(rec) else "",
        "next_occurrences": [d.isoformat() for d in upcoming],
        "commitment": cons["commitment"],
        "commitment_evidence": cons["commitment_evidence"],
        "original_start_on": (
            cons["original_start_on"].isoformat() if cons["original_start_on"] else None
        ),
        # Lugar canónico del catálogo geo (FK place_id); None si no se resolvió (o virtual).
        "place": (
            {
                "id": place.id,
                "name": place.name,
                "formatted_address": place.formatted_address,
                "lat": place.lat,
                "lng": place.lng,
            }
            if place is not None
            else None
        ),
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


# --- ocurrencias de un maestro recurrente (excepciones) ------------------------------ #


def _master_row(conn: Connection, user_id: int, cons_id: int) -> dict[str, Any]:
    cons = _live_consolidated(conn, user_id, cons_id)
    if not is_recurring(tuple(cons["recurrence"] or ())):
        raise ManualEventError(f"[{cons_id}] no es un evento recurrente (no tiene regla).")
    if not cons["series_id"]:
        raise ManualEventError(f"[{cons_id}] no tiene id de serie.")
    return cons


def _require_occurrence(cons: dict[str, Any], day: date) -> None:
    if not occurs_on(
        cons["starts_on"], tuple(cons["recurrence"]), day, start_time=cons["start_time"]
    ):
        raise ManualEventError(
            f"la serie [{cons['id']}] no tiene ocurrencia el {day.isoformat()}"
            f" ({describe(tuple(cons['recurrence']))})."
        )


def _master_event_id(conn: Connection, cons: dict[str, Any]) -> int:
    """El evento crudo MAESTRO del consolidado (el miembro con regla), para editar su RRULE."""
    row = conn.execute(
        text(
            """
            SELECT e.id FROM mod_calendar_event_links l
            JOIN mod_calendar_events e ON e.id = l.event_id
            WHERE l.consolidated_id = :cid AND cardinality(e.recurrence) > 0
            ORDER BY (e.id = :winner) DESC, e.id LIMIT 1
            """
        ),
        {"cid": int(cons["id"]), "winner": cons["winner_event_id"]},
    ).first()
    if row is None:
        raise ManualEventError(f"[{cons['id']}] no tiene un evento maestro con regla.")
    return int(row[0])


def _set_recurrence(conn: Connection, cons: dict[str, Any], recurrence: Sequence[str]) -> None:
    lines = list(validate_lines(recurrence))
    conn.execute(
        text("UPDATE mod_calendar_events SET recurrence = :r WHERE id = :id"),
        {"r": lines, "id": _master_event_id(conn, cons)},
    )
    conn.execute(
        text(
            "UPDATE mod_calendar_consolidated SET recurrence = :r, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"r": lines, "id": int(cons["id"])},
    )


def update_instance(
    user_id: int, cons_id: int, instance_on: date, changes: EventChanges
) -> dict[str, Any]:
    """Modifica UNA ocurrencia de un maestro recurrente: crea una EXCEPCIÓN (evento manual que
    apunta a la serie con `original_start_on` = la ocurrencia) y la expansión la usa en lugar de
    la ocurrencia regular. Google hace exactamente esto (`recurringEventId` + `originalStartTime`).
    `changes.starts_on` mueve la ocurrencia a otro día."""
    if changes.empty():
        raise ManualEventError("nada que actualizar: pasá al menos un campo a corregir.")
    with connection() as conn:
        cons = _master_row(conn, user_id, cons_id)
        _require_occurrence(cons, instance_on)
        existing = conn.execute(
            text(
                "SELECT id FROM mod_calendar_consolidated WHERE user_id = :u AND NOT deleted "
                "AND series_id = :s AND original_start_on = :d"
            ),
            {"u": user_id, "s": cons["series_id"], "d": instance_on},
        ).first()
        if existing is not None:
            # Ya había excepción para ese día: se corrige esa (como cualquier evento).
            result = _apply_update(conn, user_id, int(existing[0]), changes)
            run_consolidation(user_id)
            return {"mode": "exception_updated", **result, "instance_on": instance_on.isoformat()}
        new_id = _insert_manual(
            conn,
            user_id,
            title=changes.title if changes.title is not None else str(cons["title"]),
            starts_on=changes.starts_on if changes.starts_on is not None else instance_on,
            ends_on=changes.ends_on,
            start_time=(
                changes.start_time if changes.start_time is not None else cons["start_time"]
            ),
            end_time=changes.end_time if changes.end_time is not None else cons["end_time"],
            location=changes.location if changes.location is not None else str(cons["location"]),
            description=(
                changes.description if changes.description is not None else str(cons["description"])
            ),
            protected=False,
            series_id=str(cons["series_id"]),
            original_start_on=instance_on,
            original_start_time=cons["start_time"],
        )
    run_consolidation(user_id)
    _log.info(
        "calendar.manual.instance_updated",
        user_id=user_id,
        consolidated_id=cons_id,
        instance_on=instance_on.isoformat(),
        event_id=new_id,
    )
    return {
        "mode": "exception_created",
        "series_consolidated_id": cons_id,
        "event_id": new_id,
        "consolidated_ids": _consolidated_ids_of([new_id]),
        "instance_on": instance_on.isoformat(),
    }


def cancel_instance(user_id: int, cons_id: int, instance_on: date) -> dict[str, Any]:
    """Cancela UNA ocurrencia de un maestro recurrente: agrega un `EXDATE` a la regla (el resto de
    la serie sigue). Si ese día tenía una excepción, la excepción se borra también."""
    with connection() as conn:
        cons = _master_row(conn, user_id, cons_id)
        _require_occurrence(cons, instance_on)
        lines = list(cons["recurrence"])
        ex = exdate_line(instance_on)
        if ex not in lines:
            lines.append(ex)
        _set_recurrence(conn, cons, lines)
        conn.execute(
            text(
                """
                UPDATE mod_calendar_consolidated
                SET deleted = TRUE, deleted_source = 'user', updated_at = NOW()
                WHERE user_id = :u AND NOT deleted AND series_id = :s AND original_start_on = :d
                """
            ),
            {"u": user_id, "s": cons["series_id"], "d": instance_on},
        )
    run_consolidation(user_id)
    _log.info(
        "calendar.manual.instance_cancelled",
        user_id=user_id,
        consolidated_id=cons_id,
        instance_on=instance_on.isoformat(),
    )
    return {
        "mode": "instance_cancelled",
        "consolidated_id": cons_id,
        "instance_on": instance_on.isoformat(),
        "recurrence": lines,
        "recurrence_human": describe(lines),
    }


def split_series(
    user_id: int, cons_id: int, from_day: date, changes: EventChanges | None
) -> dict[str, Any]:
    """«Esta y las siguientes» (Google): desde `from_day` la serie cambia (`changes`) o termina
    (`changes=None`). El maestro viejo queda con UNTIL = día anterior; con cambios nace un maestro
    NUEVO en `from_day` con la misma regla (sin COUNT) y los campos corregidos; las excepciones
    desde `from_day` se mueven a la serie nueva (o se borran si la serie termina)."""
    if changes is not None and changes.empty():
        raise ManualEventError("nada que actualizar: pasá al menos un campo a corregir.")
    if changes is not None and changes.starts_on is not None:
        raise ManualEventError("--date no se combina con --following (la fecha la da --instance).")
    with connection() as conn:
        cons = _master_row(conn, user_id, cons_id)
        _require_occurrence(cons, from_day)
        if from_day <= cons["starts_on"]:
            # Desde la primera ocurrencia = toda la serie: no hay nada que partir.
            if changes is None:
                return remove_event(user_id, cons_id)
            return update_event(user_id, cons_id, changes)
        try:
            old_lines, new_lines = split_rrule(tuple(cons["recurrence"]), from_day)
        except RecurrenceError as e:
            raise ManualEventError(str(e)) from e
        _set_recurrence(conn, cons, old_lines)
        old_series = str(cons["series_id"])
        new_id: int | None = None
        new_series: str | None = None
        if changes is None:
            conn.execute(
                text(
                    """
                    UPDATE mod_calendar_consolidated
                    SET deleted = TRUE, deleted_source = 'user', updated_at = NOW()
                    WHERE user_id = :u AND NOT deleted AND series_id = :s
                      AND original_start_on >= :d
                    """
                ),
                {"u": user_id, "s": old_series, "d": from_day},
            )
        else:
            new_series = f"{SERIES_PREFIX}{uuid.uuid4()}"
            new_id = _insert_manual(
                conn,
                user_id,
                title=changes.title if changes.title is not None else str(cons["title"]),
                starts_on=from_day,
                ends_on=changes.ends_on,
                start_time=(
                    changes.start_time if changes.start_time is not None else cons["start_time"]
                ),
                end_time=changes.end_time if changes.end_time is not None else cons["end_time"],
                location=(
                    changes.location if changes.location is not None else str(cons["location"])
                ),
                description=(
                    changes.description
                    if changes.description is not None
                    else str(cons["description"])
                ),
                protected=False,
                series_id=new_series,
                recurrence=new_lines,
            )
            # Las excepciones de aquí en adelante pasan a la serie nueva (siguen reemplazando su
            # ocurrencia original).
            conn.execute(
                text(
                    "UPDATE mod_calendar_events SET recurring_event_id = :new "
                    "WHERE user_id = :u AND recurring_event_id = :old AND original_start_on >= :d"
                ),
                {"new": new_series, "u": user_id, "old": old_series, "d": from_day},
            )
    run_consolidation(user_id)
    _log.info(
        "calendar.manual.series_split",
        user_id=user_id,
        consolidated_id=cons_id,
        from_day=from_day.isoformat(),
        ended=changes is None,
        new_event_id=new_id,
    )
    return {
        "mode": "series_ended" if changes is None else "series_split",
        "consolidated_id": cons_id,
        "from_day": from_day.isoformat(),
        "old_recurrence": list(old_lines),
        "old_recurrence_human": describe(old_lines),
        "new_event_id": new_id,
        "new_consolidated_ids": _consolidated_ids_of([new_id]) if new_id is not None else [],
        "new_series_id": new_series,
        "new_recurrence": list(new_lines) if changes is not None else [],
    }


# --- protección --------------------------------------------------------------------- #


def set_protected(user_id: int, cons_id: int, value: bool) -> dict[str, Any]:
    """FIJA (o libera) un evento consolidado (columna `protected`): se aplica a TODOS sus miembros
    crudos vivos, así gane quien gane la consolidación el flag se conserva. Fijado = gana la
    consolidación, nada lo sobrescribe y cuenta como de alta importancia en los conflictos de
    horario. En un recurrente cubre toda la serie (el flag vive en el maestro)."""
    with connection() as conn:
        cons = _live_consolidated(conn, user_id, cons_id)
        n = conn.execute(
            text(
                """
                UPDATE mod_calendar_events e SET protected = :v
                FROM mod_calendar_event_links l
                WHERE l.event_id = e.id AND l.consolidated_id = :cid
                  AND e.provider_status IS DISTINCT FROM 'cancelled'
                """
            ),
            {"v": value, "cid": cons_id},
        ).rowcount
    run_consolidation(user_id)
    _log.info(
        "calendar.manual.protected",
        user_id=user_id,
        consolidated_id=cons_id,
        value=value,
        members=n,
    )
    return {
        "consolidated_id": cons_id,
        "title": str(cons["title"]),
        "protected": value,
        "members": int(n),
        "recurring": is_recurring(tuple(cons["recurrence"] or ())),
    }


def set_commitment_level(user_id: int, cons_id: int, level: str) -> dict[str, Any]:
    """La palabra de la persona sobre el estado: se escribe en TODOS los miembros vivos
    del consolidado (`commitment_source='user'`, así ninguna regla ni juez lo vuelve a tocar)."""
    lv = normalize_level(level, default="")
    if lv not in LEVELS:
        raise ManualEventError(f"estado inválido: {level!r} (confirmado | provisional | anuncio).")
    with connection() as conn:
        cons = _live_consolidated(conn, user_id, cons_id)
        n = conn.execute(
            text(
                """
                UPDATE mod_calendar_events e
                SET commitment = :c, commitment_source = 'user',
                    commitment_evidence = 'lo dijo la persona'
                FROM mod_calendar_event_links l
                WHERE l.event_id = e.id AND l.consolidated_id = :cid
                  AND e.provider_status IS DISTINCT FROM 'cancelled'
                """
            ),
            {"c": lv, "cid": cons_id},
        ).rowcount
    run_consolidation(user_id)
    _log.info(
        "calendar.manual.commitment", user_id=user_id, consolidated_id=cons_id, level=lv, members=n
    )
    return {"consolidated_id": cons_id, "title": str(cons["title"]), "commitment": lv, "members": n}
