"""Consolidación (ADR-015 §4, slice 3): de eventos crudos por-fuente a UN evento canónico.

Dos partes:
- PURO (testeable sin DB): `build_groups` (union-find sobre pares `confirmed` de la FASE 2 →
  componentes conexos; la transitividad se materializa SOLO sobre confirmados, respetando que la
  migración 0010 eligió pares no-grupos) + `pick_winner`/`merge_fields` (qué versión gana por
  prioridad: `protected` > `priority_rank` > más reciente; `override_policy='fill_only'` rellena
  los campos vacíos del ganador desde el resto).
- DB (`run_consolidation`): materializa `mod_calendar_consolidated` + `mod_calendar_event_links`
  de forma ESTABLE e idempotente — un `consolidated_id` no cambia entre corridas salvo merge de
  grupos (re-linkea al id menor y tombstonea los otros). Los ecos (eventos que memex escribió en
  un proveedor, con marcador `memex_consolidated_id`) se linkean a SU consolidado (evita el loop)
  y no influyen en el ganador. Marca el `processing_outcome` final de cada evento.

NO fusiona ni borra eventos crudos: `mod_calendar_events` es append/coexistencia; la consolidación
es una capa de proyección por encima.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger
from memex.modules.calendar.conflicts import ConflictEvent, find_conflicts

_log = get_logger("memex.modules.calendar.consolidate")


@dataclass(frozen=True)
class ConsEvent:
    """Un evento crudo con lo necesario para consolidar (prioridad + recencia)."""

    event_id: int
    title: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    location: str
    description: str
    priority_rank: int
    protected: bool
    override_policy: str
    recency: datetime  # provider_updated o created_at (para desempatar por más reciente)


@dataclass(frozen=True)
class ConsolidatedFields:
    """Los campos canónicos del evento consolidado + cuál evento crudo ganó."""

    title: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    location: str
    description: str
    winner_event_id: int


# --- PURO: agrupamiento + elección del ganador ------------------------------------- #


def build_groups(event_ids: Sequence[int], pairs: Sequence[tuple[int, int]]) -> list[list[int]]:
    """Union-find: agrupa `event_ids` por los `pairs` confirmados (componentes conexos).

    Determinista: cada grupo viene ordenado y la lista de grupos ordenada por su menor id.
    """
    parent: dict[int, int] = {e: e for e in event_ids}

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for a, b in pairs:
        if a in parent and b in parent:
            union(a, b)

    groups: dict[int, list[int]] = {}
    for e in event_ids:
        groups.setdefault(find(e), []).append(e)
    return [sorted(members) for _, members in sorted(groups.items())]


def _winner_sort_key(e: ConsEvent) -> tuple[int, int, float, int]:
    # Ascendente: el MEJOR primero. protegido > rank alto > más reciente > id menor (determinista).
    return (0 if e.protected else 1, -e.priority_rank, -e.recency.timestamp(), e.event_id)


def pick_winner(members: Sequence[ConsEvent]) -> ConsEvent:
    """El evento que representa al grupo: `protected` > `priority_rank` > más reciente > id."""
    return sorted(members, key=_winner_sort_key)[0]


def merge_fields(members: Sequence[ConsEvent]) -> ConsolidatedFields:
    """Campos canónicos: los del ganador; si su `override_policy='fill_only'`, rellena los vacíos
    desde el resto (en orden de prioridad)."""
    winner = pick_winner(members)
    title, starts_on = winner.title, winner.starts_on
    ends_on, start_time, end_time = winner.ends_on, winner.start_time, winner.end_time
    location, description = winner.location, winner.description

    if winner.override_policy == "fill_only":
        for m in sorted(members, key=_winner_sort_key):
            if not location and m.location:
                location = m.location
            if not description and m.description:
                description = m.description
            if end_time is None and m.end_time is not None:
                end_time = m.end_time
            if ends_on is None and m.ends_on is not None:
                ends_on = m.ends_on

    return ConsolidatedFields(
        title=title,
        starts_on=starts_on,
        ends_on=ends_on,
        start_time=start_time,
        end_time=end_time,
        location=location,
        description=description,
        winner_event_id=winner.event_id,
    )


# --- DB worker --------------------------------------------------------------------- #


@dataclass
class ConsolidationStats:
    groups: int = 0
    consolidated: int = 0  # filas de mod_calendar_consolidated insertadas o actualizadas
    merges: int = 0  # consolidados fusionados (tombstoneados) al unirse dos grupos
    echoes: int = 0  # eventos eco linkeados a su consolidado (no forman uno nuevo)
    conflicts: int = 0  # pares de eventos consolidados de alta importancia que chocan (a revisar)


def _load_events(
    conn: Connection, user_id: int, existing_cons: set[int]
) -> tuple[list[ConsEvent], dict[int, int]]:
    """Carga los eventos NO cancelados. Devuelve (normales, ecos {event_id: consolidated_id}).

    Un evento es ECO si su metadata trae `memex_consolidated_id` apuntando a un consolidado que YA
    existe (lo escribió memex) → se linkea ahí, no forma grupo. Si el consolidado no existe, cae a
    normal."""
    rows = (
        conn.execute(
            text(
                """
                SELECT id, title, starts_on, ends_on, start_time, end_time, location, description,
                       priority_rank, protected, override_policy,
                       COALESCE(provider_updated, created_at) AS recency,
                       metadata->>'memex_consolidated_id' AS mcid
                FROM mod_calendar_events
                WHERE user_id = :uid AND provider_status IS DISTINCT FROM 'cancelled'
                ORDER BY id
                """
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    normal: list[ConsEvent] = []
    echo: dict[int, int] = {}
    for r in rows:
        eid = int(r["id"])
        mcid_raw = r["mcid"]
        if mcid_raw is not None and int(mcid_raw) in existing_cons:
            echo[eid] = int(mcid_raw)
            continue
        normal.append(
            ConsEvent(
                event_id=eid,
                title=str(r["title"]),
                starts_on=r["starts_on"],
                ends_on=r["ends_on"],
                start_time=r["start_time"],
                end_time=r["end_time"],
                location=str(r["location"]),
                description=str(r["description"]),
                priority_rank=int(r["priority_rank"]),
                protected=bool(r["protected"]),
                override_policy=str(r["override_policy"]),
                recency=r["recency"],
            )
        )
    return normal, echo


def _confirmed_pairs(conn: Connection, user_id: int) -> list[tuple[int, int]]:
    rows = conn.execute(
        text(
            "SELECT event_a_id, event_b_id FROM mod_calendar_dedup_candidates "
            "WHERE user_id = :uid AND status = 'confirmed'"
        ),
        {"uid": user_id},
    ).all()
    return [(int(a), int(b)) for a, b in rows]


def _pending_event_ids(conn: Connection, user_id: int) -> set[int]:
    rows = conn.execute(
        text(
            "SELECT event_a_id, event_b_id FROM mod_calendar_dedup_candidates "
            "WHERE user_id = :uid AND status = 'candidate'"
        ),
        {"uid": user_id},
    ).all()
    pending: set[int] = set()
    for a, b in rows:
        pending.update((int(a), int(b)))
    return pending


def _write_consolidated(
    conn: Connection, user_id: int, cons_id: int | None, fields: ConsolidatedFields
) -> int:
    params = {
        "uid": user_id,
        "title": fields.title,
        "starts_on": fields.starts_on,
        "ends_on": fields.ends_on,
        "start_time": fields.start_time,
        "end_time": fields.end_time,
        "location": fields.location,
        "description": fields.description,
        "winner": fields.winner_event_id,
    }
    if cons_id is None:
        return int(
            conn.execute(
                text(
                    """
                    INSERT INTO mod_calendar_consolidated
                      (user_id, title, starts_on, ends_on, start_time, end_time, location,
                       description, winner_event_id)
                    VALUES
                      (:uid, :title, :starts_on, :ends_on, :start_time, :end_time, :location,
                       :description, :winner)
                    RETURNING id
                    """
                ),
                params,
            ).scalar_one()
        )
    conn.execute(
        text(
            """
            UPDATE mod_calendar_consolidated SET
              title = :title, starts_on = :starts_on, ends_on = :ends_on,
              start_time = :start_time, end_time = :end_time, location = :location,
              description = :description, winner_event_id = :winner, deleted = FALSE,
              updated_at = NOW()
            WHERE id = :id
            """
        ),
        {**params, "id": cons_id},
    )
    return cons_id


def _link(conn: Connection, user_id: int, cons_id: int, event_id: int) -> None:
    conn.execute(
        text(
            """
            INSERT INTO mod_calendar_event_links (user_id, consolidated_id, event_id)
            VALUES (:uid, :cid, :eid)
            ON CONFLICT (event_id) DO UPDATE SET consolidated_id = EXCLUDED.consolidated_id
            """
        ),
        {"uid": user_id, "cid": cons_id, "eid": event_id},
    )


def _detect_conflicts(conn: Connection, user_id: int) -> int:
    """Detecta pares de eventos CONSOLIDADOS de alta importancia que CHOCAN en el horario y los
    encola en `mod_calendar_conflicts` (pendiente de revisión). La importancia sale del ganador de
    cada consolidado. Reconcilia los 'pending' (borra los que ya no aplican); preserva las
    decisiones humanas ('resolved'/'dismissed') con `ON CONFLICT DO NOTHING` al re-insertar."""
    rows = (
        conn.execute(
            text(
                """
                SELECT c.id, c.starts_on, c.ends_on, c.start_time, c.end_time,
                       e.priority_rank, e.protected
                FROM mod_calendar_consolidated c
                JOIN mod_calendar_events e ON e.id = c.winner_event_id
                WHERE c.user_id = :uid AND NOT c.deleted
                """
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )
    events = [
        ConflictEvent(
            consolidated_id=int(r["id"]),
            starts_on=r["starts_on"],
            ends_on=r["ends_on"],
            start_time=r["start_time"],
            end_time=r["end_time"],
            priority_rank=int(r["priority_rank"]),
            protected=bool(r["protected"]),
        )
        for r in rows
    ]
    pairs = find_conflicts(events)
    detected = set(pairs)
    # Reconciliar los PENDIENTES: borrar los conflictos que ya no se detectan (el criterio cambió,
    # o el solape desapareció al re-consolidar). Preserva 'resolved'/'dismissed' (decisiones
    # humanas) — solo toca 'pending'. Así re-correr la consolidación auto-corrige el ruido viejo.
    existing_pending = {
        (int(a), int(b))
        for a, b in conn.execute(
            text(
                "SELECT consolidated_a_id, consolidated_b_id FROM mod_calendar_conflicts "
                "WHERE user_id = :uid AND status = 'pending'"
            ),
            {"uid": user_id},
        ).all()
    }
    for a, b in existing_pending - detected:
        conn.execute(
            text(
                "DELETE FROM mod_calendar_conflicts WHERE user_id = :uid "
                "AND consolidated_a_id = :a AND consolidated_b_id = :b AND status = 'pending'"
            ),
            {"uid": user_id, "a": a, "b": b},
        )
    for a, b in pairs:
        conn.execute(
            text(
                """
                INSERT INTO mod_calendar_conflicts
                  (user_id, consolidated_a_id, consolidated_b_id, reason)
                VALUES (:uid, :a, :b, 'time_overlap_high_priority')
                ON CONFLICT (consolidated_a_id, consolidated_b_id) DO NOTHING
                """
            ),
            {"uid": user_id, "a": a, "b": b},
        )
    return len(pairs)


def _set_outcomes(conn: Connection, outcome_to_ids: dict[str, list[int]]) -> None:
    for outcome, ids in outcome_to_ids.items():
        if ids:
            conn.execute(
                text("UPDATE mod_calendar_events SET processing_outcome = :o WHERE id = ANY(:ids)"),
                {"o": outcome, "ids": ids},
            )


def run_consolidation(user_id: int) -> ConsolidationStats:
    """Reconstruye la proyección consolidada del user de forma estable e idempotente."""
    stats = ConsolidationStats()
    with connection() as conn:
        existing_cons = {
            int(r[0])
            for r in conn.execute(
                text("SELECT id FROM mod_calendar_consolidated WHERE user_id = :uid"),
                {"uid": user_id},
            ).all()
        }
        normal, echo = _load_events(conn, user_id, existing_cons)
        normal_ids = {e.event_id for e in normal}
        by_id = {e.event_id: e for e in normal}
        pairs = [
            (a, b)
            for a, b in _confirmed_pairs(conn, user_id)
            if a in normal_ids and b in normal_ids
        ]
        pending_ids = _pending_event_ids(conn, user_id)

        groups = build_groups(sorted(normal_ids), pairs)
        outcomes: dict[str, list[int]] = {"unique": [], "duplicate": [], "echo": []}

        for group_ids in groups:
            stats.groups += 1
            members = [by_id[i] for i in group_ids]
            fields = merge_fields(members)

            existing = sorted(
                {
                    int(r[0])
                    for r in conn.execute(
                        text(
                            "SELECT DISTINCT consolidated_id FROM mod_calendar_event_links "
                            "WHERE event_id = ANY(:ids)"
                        ),
                        {"ids": group_ids},
                    ).all()
                }
            )
            if not existing:
                cons_id = _write_consolidated(conn, user_id, None, fields)
                stats.consolidated += 1
            else:
                cons_id = existing[0]
                changed = len(existing) > 1  # fusión de grupos → siempre reescribe
                if changed:  # un par confirmado unió dos grupos previos → fusionar
                    others = existing[1:]
                    conn.execute(
                        text(
                            "UPDATE mod_calendar_event_links SET consolidated_id = :keep "
                            "WHERE consolidated_id = ANY(:others)"
                        ),
                        {"keep": cons_id, "others": others},
                    )
                    conn.execute(
                        text(
                            "UPDATE mod_calendar_consolidated "
                            "SET deleted = TRUE, updated_at = NOW() WHERE id = ANY(:others)"
                        ),
                        {"others": others},
                    )
                    stats.merges += len(others)
                else:
                    # ¿cambió la membresía del consolidado? Si NO, dejamos sus campos tal cual
                    # (estable): no pisamos el enriquecimiento del LLM (paso `merge`) ni churnéamos
                    # la firma del write-back en cada corrida.
                    current = {
                        int(r[0])
                        for r in conn.execute(
                            text(
                                "SELECT event_id FROM mod_calendar_event_links "
                                "WHERE consolidated_id = :cid"
                            ),
                            {"cid": cons_id},
                        ).all()
                    }
                    changed = set(group_ids) != current
                if changed:
                    _write_consolidated(conn, user_id, cons_id, fields)
                    stats.consolidated += 1

            for mid in group_ids:
                _link(conn, user_id, cons_id, mid)

            # outcomes del grupo (los que aún tienen un par 'candidate' sin resolver quedan pending)
            for mid in group_ids:
                if mid in pending_ids:
                    continue
                if len(group_ids) > 1 and mid != fields.winner_event_id:
                    outcomes["duplicate"].append(mid)
                else:
                    outcomes["unique"].append(mid)

        for eid, cons_id in echo.items():
            _link(conn, user_id, cons_id, eid)
            outcomes["echo"].append(eid)
            stats.echoes += 1

        _set_outcomes(conn, outcomes)
        stats.conflicts = _detect_conflicts(conn, user_id)

    _log.info(
        "calendar.consolidate.done",
        user_id=user_id,
        groups=stats.groups,
        consolidated=stats.consolidated,
        merges=stats.merges,
        echoes=stats.echoes,
        conflicts=stats.conflicts,
    )
    return stats
