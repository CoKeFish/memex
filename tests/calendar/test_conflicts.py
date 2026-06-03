"""Conflictos: detección pura (`find_conflicts`) + integración en la consolidación (DB).

Un conflicto = dos eventos CONSOLIDADOS distintos que se solapan en el tiempo Y ambos son de alta
importancia (protected o rank≥50). NUNCA fusiona ni descarta: solo encola en mod_calendar_conflicts.
"""

from __future__ import annotations

from datetime import date, time

from sqlalchemy import text

from memex.db import connection
from memex.modules.calendar.conflicts import ConflictEvent, find_conflicts
from memex.modules.calendar.consolidate import run_consolidation

# ----- puro: find_conflicts ------------------------------------------------------ #


def _cf(
    cid: int,
    *,
    start_time: time | None = time(10, 0),
    end_time: time | None = None,
    starts_on: date = date(2026, 6, 3),
    ends_on: date | None = None,
    priority_rank: int = 0,
    protected: bool = False,
) -> ConflictEvent:
    return ConflictEvent(
        consolidated_id=cid,
        starts_on=starts_on,
        ends_on=ends_on,
        start_time=start_time,
        end_time=end_time,
        priority_rank=priority_rank,
        protected=protected,
    )


def test_two_high_importance_overlap_is_conflict() -> None:
    a = _cf(1, start_time=time(10, 0), protected=True)
    b = _cf(2, start_time=time(10, 20), priority_rank=100)
    assert find_conflicts([a, b]) == [(1, 2)]


def test_both_must_be_high_importance() -> None:
    a = _cf(1, start_time=time(10, 0), priority_rank=100)
    b = _cf(2, start_time=time(10, 20), priority_rank=0)  # trivial → no conflicto
    assert find_conflicts([a, b]) == []


def test_high_importance_but_disjoint_times_no_conflict() -> None:
    a = _cf(1, start_time=time(9, 0), protected=True)
    b = _cf(2, start_time=time(18, 0), protected=True)
    assert find_conflicts([a, b]) == []


def test_protected_counts_as_high_even_rank_zero() -> None:
    a = _cf(1, start_time=time(10, 0), protected=True, priority_rank=0)
    b = _cf(2, start_time=time(10, 10), protected=True, priority_rank=0)
    assert find_conflicts([a, b]) == [(1, 2)]


def test_all_day_event_does_not_conflict_with_timed() -> None:
    # Un evento todo-el-día (cumpleaños/feriado) no bloquea un horario → no choca con una clase.
    allday = _cf(1, start_time=None, protected=True)
    clase = _cf(2, start_time=time(10, 0), protected=True)
    assert find_conflicts([allday, clase]) == []


def test_two_all_day_same_day_no_conflict() -> None:
    a = _cf(1, start_time=None, protected=True)
    b = _cf(2, start_time=None, protected=True)
    assert find_conflicts([a, b]) == []


def test_multi_day_event_does_not_conflict_with_timed() -> None:
    # Multi-día (ends_on != None): no bloquea un horario puntual.
    viaje = _cf(1, start_time=time(8, 0), ends_on=date(2026, 6, 5), protected=True)
    clase = _cf(2, start_time=time(10, 0), starts_on=date(2026, 6, 4), protected=True)
    assert find_conflicts([viaje, clase]) == []


def test_timed_events_with_real_gap_no_conflict() -> None:
    # 10:00-10:30 y 11:00-11:30 no se pisan; antes el margen difuso de 30 min los marcaba.
    a = _cf(1, start_time=time(10, 0), end_time=time(10, 30), protected=True)
    b = _cf(2, start_time=time(11, 0), end_time=time(11, 30), protected=True)
    assert find_conflicts([a, b]) == []


def test_timed_events_truly_overlapping_conflict() -> None:
    a = _cf(1, start_time=time(10, 0), end_time=time(11, 0), protected=True)
    b = _cf(2, start_time=time(10, 30), end_time=time(11, 30), protected=True)
    assert find_conflicts([a, b]) == [(1, 2)]


# ----- DB: detección dentro de run_consolidation --------------------------------- #


def _seed(
    title: str,
    *,
    start_time: time | None,
    priority_rank: int = 0,
    protected: bool = False,
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, start_time,
                       priority_rank, protected)
                    VALUES (1, ARRAY[]::bigint[], :t, :d, :st, :pr, :prot)
                    RETURNING id
                    """
                ),
                {
                    "t": title,
                    "d": date(2026, 6, 3),
                    "st": start_time,
                    "pr": priority_rank,
                    "prot": protected,
                },
            ).scalar_one()
        )


def _confirm(a: int, b: int) -> None:
    lo, hi = min(a, b), max(a, b)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_dedup_candidates "
                "(user_id, event_a_id, event_b_id, reason, score, status) "
                "VALUES (1, :a, :b, 'time+title', 0.9, 'confirmed')"
            ),
            {"a": lo, "b": hi},
        )


def _pending_conflicts() -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "SELECT COUNT(*) FROM mod_calendar_conflicts "
                    "WHERE user_id = 1 AND status = 'pending'"
                )
            ).scalar_one()
        )


def _consolidated_id(event_id: int) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("SELECT consolidated_id FROM mod_calendar_event_links WHERE event_id = :e"),
                {"e": event_id},
            ).scalar_one()
        )


def test_consolidation_enqueues_high_priority_clash() -> None:
    _seed("Clase de Cálculo", start_time=time(10, 0), priority_rank=100)
    _seed("Turno médico", start_time=time(10, 20), priority_rank=100)  # distinto evento, choca
    stats = run_consolidation(1)
    assert stats.conflicts == 1
    assert _pending_conflicts() == 1


def test_consolidation_ignores_low_priority_clash() -> None:
    _seed("recordatorio A", start_time=time(10, 0), priority_rank=0)
    _seed("recordatorio B", start_time=time(10, 20), priority_rank=0)
    stats = run_consolidation(1)
    assert stats.conflicts == 0
    assert _pending_conflicts() == 0


def test_consolidation_conflict_idempotent() -> None:
    _seed("Clase", start_time=time(10, 0), protected=True)
    _seed("Turno", start_time=time(10, 20), protected=True)
    run_consolidation(1)
    run_consolidation(1)  # re-run no duplica el conflicto
    assert _pending_conflicts() == 1


def test_consolidation_excludes_all_day_clash() -> None:
    # Cumpleaños todo-el-día (rank 100) + clase con hora (rank 100): ambos de alta importancia,
    # pero el todo-el-día no bloquea horario → NO debe encolar conflicto.
    _seed("Cumpleaños", start_time=None, priority_rank=100)
    _seed("Clase de Cálculo", start_time=time(10, 0), priority_rank=100)
    stats = run_consolidation(1)
    assert stats.conflicts == 0
    assert _pending_conflicts() == 0


def test_conflict_reconcile_removes_stale_pending() -> None:
    # Choque real → 1 pendiente. Luego un evento se mueve a un horario disjunto: re-consolidar
    # debe BORRAR el conflicto pendiente que ya no aplica (reconciliación).
    _seed("Clase", start_time=time(10, 0), priority_rank=100)
    moved = _seed("Turno médico", start_time=time(10, 20), priority_rank=100)
    run_consolidation(1)
    assert _pending_conflicts() == 1
    with connection() as c:
        c.execute(
            text("UPDATE mod_calendar_consolidated SET start_time = '18:00' WHERE id = :id"),
            {"id": _consolidated_id(moved)},
        )
    run_consolidation(1)
    assert _pending_conflicts() == 0


def test_conflict_reconcile_preserves_human_decision() -> None:
    # Un conflicto 'dismissed' (decisión humana) NO se borra ni se reabre al re-consolidar.
    _seed("Clase", start_time=time(10, 0), protected=True)
    _seed("Turno", start_time=time(10, 20), protected=True)
    run_consolidation(1)
    with connection() as c:
        c.execute(text("UPDATE mod_calendar_conflicts SET status = 'dismissed' WHERE user_id = 1"))
    run_consolidation(1)
    assert _pending_conflicts() == 0
    with connection() as c:
        dismissed = int(
            c.execute(
                text(
                    "SELECT COUNT(*) FROM mod_calendar_conflicts "
                    "WHERE user_id = 1 AND status = 'dismissed'"
                )
            ).scalar_one()
        )
    assert dismissed == 1


def test_protected_event_wins_consolidation() -> None:
    ext = _seed("Reunión", start_time=time(9, 0), priority_rank=0)
    cls = _seed("Clase", start_time=time(9, 0), priority_rank=1000, protected=True)
    _confirm(ext, cls)  # el dedup los dio por el mismo → el protegido debe ganar
    run_consolidation(1)
    with connection() as c:
        winner = c.execute(
            text(
                "SELECT winner_event_id FROM mod_calendar_consolidated "
                "WHERE user_id = 1 AND NOT deleted"
            )
        ).scalar_one()
    assert int(winner) == cls
