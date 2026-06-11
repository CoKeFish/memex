"""Consolidación: algoritmos puros (union-find / ganador / merge) + worker contra la DB.

Cubre: agrupamiento transitivo solo sobre confirmados, elección de ganador por prioridad,
`fill_only`, materialización estable e idempotente, fusión de grupos (tombstone), eventos
`pending` que conservan su outcome, y ecos que se linkean a su consolidado (no forman uno nuevo).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.calendar.consolidate import (
    ConsEvent,
    build_groups,
    merge_fields,
    pick_winner,
    run_consolidation,
)
from memex.modules.calendar.domain import CalendarDomainReader

# ----- puro: build_groups -------------------------------------------------------- #


def test_build_groups_transitive() -> None:
    # A~B y B~C confirmados → un solo grupo {1,2,3}.
    groups = build_groups([1, 2, 3], [(1, 2), (2, 3)])
    assert groups == [[1, 2, 3]]


def test_build_groups_disjoint() -> None:
    groups = build_groups([1, 2, 3, 4], [(1, 2)])
    assert groups == [[1, 2], [3], [4]]


def test_build_groups_no_pairs_all_singletons() -> None:
    assert build_groups([5, 9, 7], []) == [[5], [7], [9]]


# ----- puro: pick_winner / merge_fields ------------------------------------------ #


def _ce(
    event_id: int,
    *,
    title: str = "Evento",
    priority_rank: int = 0,
    protected: bool = False,
    override_policy: str = "replace",
    location: str = "",
    description: str = "",
    end_time: time | None = None,
    recency: datetime | None = None,
) -> ConsEvent:
    return ConsEvent(
        event_id=event_id,
        title=title,
        starts_on=date(2026, 6, 3),
        ends_on=None,
        start_time=time(10, 0),
        end_time=end_time,
        location=location,
        description=description,
        priority_rank=priority_rank,
        protected=protected,
        override_policy=override_policy,
        recency=recency or datetime(2026, 5, 1, tzinfo=UTC),
    )


def test_pick_winner_protected_beats_rank() -> None:
    winner = pick_winner([_ce(1, priority_rank=100), _ce(2, protected=True, priority_rank=0)])
    assert winner.event_id == 2


def test_pick_winner_higher_rank() -> None:
    assert pick_winner([_ce(1, priority_rank=0), _ce(2, priority_rank=50)]).event_id == 2


def test_pick_winner_recency_tiebreak() -> None:
    older = _ce(1, recency=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _ce(2, recency=datetime(2026, 5, 20, tzinfo=UTC))
    assert pick_winner([older, newer]).event_id == 2


def test_merge_fields_fill_only_fills_empty() -> None:
    winner = _ce(1, title="Cita", override_policy="fill_only", location="", end_time=None)
    other = _ce(2, location="Centro", end_time=time(11, 0))
    fields = merge_fields([winner, other])
    assert fields.winner_event_id == 1
    assert fields.location == "Centro"  # rellenó el vacío del ganador
    assert fields.end_time == time(11, 0)


def test_merge_fields_replace_keeps_winner_only() -> None:
    winner = _ce(1, title="Cita", override_policy="replace", location="")
    other = _ce(2, location="Centro")
    fields = merge_fields([winner, other])
    assert fields.location == ""  # replace NO rellena desde otros


# ----- DB: run_consolidation ----------------------------------------------------- #


def _seed(
    title: str,
    *,
    start_time: time | None = None,
    starts_on: date = date(2026, 6, 3),
    priority_rank: int = 0,
    protected: bool = False,
    override_policy: str = "replace",
    location: str = "",
    metadata: str = "{}",
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, start_time, location,
                       priority_rank, protected, override_policy, metadata)
                    VALUES (1, ARRAY[]::bigint[], :t, :d, :st, :loc, :pr, :prot, :op,
                            CAST(:meta AS JSONB))
                    RETURNING id
                    """
                ),
                {
                    "t": title,
                    "d": starts_on,
                    "st": start_time,
                    "loc": location,
                    "pr": priority_rank,
                    "prot": protected,
                    "op": override_policy,
                    "meta": metadata,
                },
            ).scalar_one()
        )


def _pair(a: int, b: int, *, status: str) -> None:
    lo, hi = min(a, b), max(a, b)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_dedup_candidates "
                "(user_id, event_a_id, event_b_id, reason, score, status) "
                "VALUES (1, :a, :b, 'time+title', 0.9, :s)"
            ),
            {"a": lo, "b": hi, "s": status},
        )


def _consolidated() -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT id, winner_event_id, deleted, deleted_source "
                    "FROM mod_calendar_consolidated WHERE user_id = 1 ORDER BY id"
                )
            )
            .mappings()
            .all()
        ]


def _link_of(event_id: int) -> int | None:
    with connection() as c:
        val = c.execute(
            text("SELECT consolidated_id FROM mod_calendar_event_links WHERE event_id = :e"),
            {"e": event_id},
        ).first()
    return int(val[0]) if val is not None else None


def _outcome(event_id: int) -> str:
    with connection() as c:
        return str(
            c.execute(
                text("SELECT processing_outcome FROM mod_calendar_events WHERE id = :e"),
                {"e": event_id},
            ).scalar_one()
        )


def test_consolidation_singletons_each_own_consolidated() -> None:
    a = _seed("Dentista", start_time=time(9, 0))
    b = _seed("Gimnasio", start_time=time(18, 0), starts_on=date(2026, 7, 1))
    run_consolidation(1)

    cons = _consolidated()
    assert len(cons) == 2
    assert _outcome(a) == "unique"
    assert _outcome(b) == "unique"
    assert _link_of(a) != _link_of(b)


def test_consolidation_confirmed_pair_one_consolidated() -> None:
    a = _seed("Dentista", start_time=time(10, 0))
    b = _seed("Cita Dentalink", start_time=time(10, 0), priority_rank=10)  # b gana
    _pair(a, b, status="confirmed")
    run_consolidation(1)

    cons = [c for c in _consolidated() if not c["deleted"]]
    assert len(cons) == 1
    assert cons[0]["winner_event_id"] == b
    assert _link_of(a) == _link_of(b) == cons[0]["id"]
    assert _outcome(b) == "unique"
    assert _outcome(a) == "duplicate"

    with connection() as c:
        events = CalendarDomainReader(c, 1).events_in_range(date(2026, 6, 1), date(2026, 6, 30))
    assert len(events) == 1  # la vista consolidada muestra UN evento


def test_consolidation_manual_high_priority_wins() -> None:
    extracted = _seed("Reunión", start_time=time(15, 0), priority_rank=0)
    manual = _seed("Reunion", start_time=time(15, 0), priority_rank=100)  # manual
    _pair(extracted, manual, status="confirmed")
    run_consolidation(1)

    cons = [c for c in _consolidated() if not c["deleted"]]
    assert cons[0]["winner_event_id"] == manual


def test_consolidation_is_idempotent() -> None:
    a = _seed("Dentista", start_time=time(10, 0))
    b = _seed("Cita Dentalink", start_time=time(10, 0))
    _pair(a, b, status="confirmed")
    run_consolidation(1)
    cons_id_first = _link_of(a)
    run_consolidation(1)  # re-run

    cons = [c for c in _consolidated() if not c["deleted"]]
    assert len(cons) == 1  # no duplicó
    assert _link_of(a) == cons_id_first  # consolidated_id estable


def test_consolidation_merges_groups_on_new_confirmed_pair() -> None:
    a = _seed("Dentista", start_time=time(10, 0))
    b = _seed("Cita Dentalink", start_time=time(10, 0))
    run_consolidation(1)  # sin pares → 2 consolidados separados
    assert len([c for c in _consolidated() if not c["deleted"]]) == 2

    _pair(a, b, status="confirmed")  # ahora se confirma que son el mismo
    run_consolidation(1)

    active = [c for c in _consolidated() if not c["deleted"]]
    tombstoned = [c for c in _consolidated() if c["deleted"]]
    assert len(active) == 1
    assert len(tombstoned) == 1
    assert _link_of(a) == _link_of(b) == active[0]["id"]


def test_consolidation_pending_keeps_outcome_pending() -> None:
    a = _seed("X", start_time=time(9, 0))
    b = _seed("Y", start_time=time(9, 0))
    _pair(a, b, status="candidate")  # sin resolver por F2
    run_consolidation(1)

    assert _outcome(a) == "pending"
    assert _outcome(b) == "pending"


def test_consolidation_echo_links_to_existing_consolidated() -> None:
    # Consolidado pre-existente (como si memex lo hubiera creado y pusheado).
    with connection() as c:
        cons_id = int(
            c.execute(
                text(
                    "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on) "
                    "VALUES (1, 'Evento memex', :d) RETURNING id"
                ),
                {"d": date(2026, 6, 3)},
            ).scalar_one()
        )
    echo = _seed(
        "Evento memex",
        start_time=time(10, 0),
        metadata=f'{{"memex_consolidated_id": {cons_id}}}',
    )
    run_consolidation(1)

    assert _link_of(echo) == cons_id  # se linkeó al consolidado existente
    assert _outcome(echo) == "echo"
    # No se creó un consolidado NUEVO para el eco.
    assert len(_consolidated()) == 1


# ----- DB: tombstones (huérfanos / fusión / borrado del usuario, 0059) ------------ #


def _set_cancelled(event_id: int, *, cancelled: bool = True) -> None:
    with connection() as c:
        c.execute(
            text("UPDATE mod_calendar_events SET provider_status = :s WHERE id = :i"),
            {"s": "cancelled" if cancelled else "confirmed", "i": event_id},
        )


def _conflict_pendings() -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "SELECT count(*) FROM mod_calendar_conflicts "
                    "WHERE user_id = 1 AND status = 'pending'"
                )
            ).scalar_one()
        )


def test_orphan_tombstone_when_members_deleted() -> None:
    # El caso del incidente: los crudos se borraron (links en cascada) y el consolidado quedaba
    # vivo para siempre.
    a = _seed("Clase (Google)", start_time=time(8, 0))
    run_consolidation(1)
    cons_id = _link_of(a)
    with connection() as c:
        c.execute(text("DELETE FROM mod_calendar_events WHERE id = :i"), {"i": a})

    stats = run_consolidation(1)

    assert stats.orphans == 1
    row = next(r for r in _consolidated() if r["id"] == cons_id)
    assert row["deleted"] is True
    assert row["deleted_source"] == "orphaned"


def test_orphan_tombstone_purges_pending_conflicts() -> None:
    # Dos protegidos que chocan → conflicto pending; al quedar huérfanos ambos, la reconciliación
    # de la MISMA corrida purga el conflicto.
    a = _seed("Clase A", start_time=time(10, 0), protected=True)
    b = _seed("Turno B", start_time=time(10, 30), protected=True)
    run_consolidation(1)
    assert _conflict_pendings() == 1

    with connection() as c:
        c.execute(text("DELETE FROM mod_calendar_events WHERE id = ANY(:ids)"), {"ids": [a, b]})
    stats = run_consolidation(1)

    assert stats.orphans == 2
    assert _conflict_pendings() == 0


def test_orphan_tombstone_when_all_members_cancelled_and_revives() -> None:
    a = _seed("Reunión cancelable", start_time=time(11, 0))
    run_consolidation(1)
    cons_id = _link_of(a)

    _set_cancelled(a)
    run_consolidation(1)
    row = next(r for r in _consolidated() if r["id"] == cons_id)
    assert (row["deleted"], row["deleted_source"]) == (True, "orphaned")

    # El proveedor lo restaura → el MISMO consolidado revive (membresía idéntica: el guard de
    # estabilidad no alcanza, lo cubre el camino de revival de tombstones automáticos).
    _set_cancelled(a, cancelled=False)
    run_consolidation(1)
    row = next(r for r in _consolidated() if r["id"] == cons_id)
    assert (row["deleted"], row["deleted_source"]) == (False, None)


def test_merge_tombstone_marks_source_merge() -> None:
    a = _seed("Dentista", start_time=time(10, 0))
    b = _seed("Cita Dentalink", start_time=time(10, 0))
    run_consolidation(1)
    _pair(a, b, status="confirmed")
    run_consolidation(1)

    tombstoned = [c for c in _consolidated() if c["deleted"]]
    assert len(tombstoned) == 1
    assert tombstoned[0]["deleted_source"] == "merge"


def test_user_tombstone_never_resurrected() -> None:
    a = _seed("Dentista", start_time=time(10, 0))
    run_consolidation(1)
    cons_id = _link_of(a)
    assert cons_id is not None
    with connection() as c:
        c.execute(
            text(
                "UPDATE mod_calendar_consolidated SET deleted = TRUE, deleted_source = 'user' "
                "WHERE id = :i"
            ),
            {"i": cons_id},
        )

    # Reconsolidación sin cambios de membresía: sigue muerto.
    run_consolidation(1)
    row = next(r for r in _consolidated() if r["id"] == cons_id)
    assert (row["deleted"], row["deleted_source"]) == (True, "user")

    # Cambia la membresía (par confirmado suma un miembro) → se reescribe, pero NO revive.
    b = _seed("Cita Dentalink", start_time=time(10, 0))
    _pair(a, b, status="confirmed")
    run_consolidation(1)
    row = next(r for r in _consolidated() if r["id"] == cons_id)
    assert (row["deleted"], row["deleted_source"]) == (True, "user")
