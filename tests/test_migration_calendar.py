"""Schema check para las tablas de calendar (migraciones 0010/0011/0059).

Verifica que el DDL impone lo diseñado: array de atribución de `mod_calendar_events`, cascadas
a users, en `mod_calendar_dedup_candidates` el par canónico (CHECK a<b), el UNIQUE del par,
el CHECK de status y las cascadas (al borrar evento y al borrar user); y de 0059 el origin
`'manual'` + la coherencia `deleted`↔`deleted_source` del consolidado.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection

# ----- mod_calendar_events ------------------------------------------------------- #


def _seed_event(user_id: int = 1, starts_on: date = date(2026, 6, 3)) -> int:
    with connection() as c:
        eid = c.execute(
            text(
                "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on) "
                "VALUES (:u, ARRAY[]::bigint[], 'X', :d) RETURNING id"
            ),
            {"u": user_id, "d": starts_on},
        ).scalar_one()
    return int(eid)


def test_calendar_event_insert_and_read_back() -> None:
    with connection() as c:
        row = (
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, location, evidence)
                    VALUES (1, ARRAY[7, 9]::bigint[], 'Examen', DATE '2026-06-03', 'Aula 7',
                            'el examen es el 3/6')
                    RETURNING title, starts_on, start_time, source_inbox_ids
                    """
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["title"] == "Examen"
    assert row["starts_on"] == date(2026, 6, 3)
    assert row["start_time"] is None  # sin hora → todo el día
    assert list(row["source_inbox_ids"]) == [7, 9]


def test_calendar_event_cascade_on_user_delete(seed_user2: int) -> None:
    _seed_event(seed_user2)
    with connection() as c:
        c.execute(text("DELETE FROM users WHERE id = :u"), {"u": seed_user2})
        remaining = c.execute(
            text("SELECT count(*) FROM mod_calendar_events WHERE user_id = :u"), {"u": seed_user2}
        ).scalar()
    assert remaining == 0


def test_calendar_event_accepts_manual_origin() -> None:
    with connection() as c:
        origin = c.execute(
            text(
                "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on, "
                "origin, manual) VALUES (1, ARRAY[]::bigint[], 'Cita', DATE '2026-06-20', "
                "'manual', TRUE) RETURNING origin"
            )
        ).scalar_one()
    assert origin == "manual"


def test_calendar_event_rejects_unknown_origin() -> None:
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on, "
                "origin) VALUES (1, ARRAY[]::bigint[], 'X', DATE '2026-06-20', 'nonsense')"
            )
        )


# ----- mod_calendar_consolidated: coherencia deleted ↔ deleted_source (0059) ------ #


def _insert_consolidated(*, deleted: bool, deleted_source: str | None) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on, deleted, "
                "deleted_source) VALUES (1, 'X', DATE '2026-06-20', :d, :src)"
            ),
            {"d": deleted, "src": deleted_source},
        )


def test_consolidated_tombstone_requires_source() -> None:
    with pytest.raises(IntegrityError):
        _insert_consolidated(deleted=True, deleted_source=None)


def test_consolidated_alive_rejects_source() -> None:
    with pytest.raises(IntegrityError):
        _insert_consolidated(deleted=False, deleted_source="user")


def test_consolidated_rejects_unknown_source() -> None:
    with pytest.raises(IntegrityError):
        _insert_consolidated(deleted=True, deleted_source="nonsense")


def test_consolidated_coherent_pairs_accepted() -> None:
    _insert_consolidated(deleted=False, deleted_source=None)
    for src in ("merge", "orphaned", "user"):
        _insert_consolidated(deleted=True, deleted_source=src)


# ----- mod_calendar_dedup_candidates --------------------------------------------- #


def _insert_pair(a: int, b: int, *, status: str = "candidate") -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_dedup_candidates "
                "(user_id, event_a_id, event_b_id, reason, status) "
                "VALUES (1, :a, :b, 'time+title', :s)"
            ),
            {"a": a, "b": b, "s": status},
        )


def test_dedup_candidate_unique_pair() -> None:
    a, b = _seed_event(), _seed_event()
    _insert_pair(min(a, b), max(a, b))
    with pytest.raises(IntegrityError):
        _insert_pair(min(a, b), max(a, b))


def test_dedup_candidate_rejects_reversed_order() -> None:
    a, b = _seed_event(), _seed_event()
    with pytest.raises(IntegrityError):  # CHECK (event_a_id < event_b_id)
        _insert_pair(max(a, b), min(a, b))


def test_dedup_candidate_rejects_bad_status() -> None:
    a, b = _seed_event(), _seed_event()
    with pytest.raises(IntegrityError):
        _insert_pair(min(a, b), max(a, b), status="nonsense")


def test_dedup_candidate_cascade_on_event_delete() -> None:
    a, b = _seed_event(), _seed_event()
    lo, hi = min(a, b), max(a, b)
    _insert_pair(lo, hi)
    with connection() as c:
        c.execute(text("DELETE FROM mod_calendar_events WHERE id = :i"), {"i": lo})
        n = c.execute(text("SELECT count(*) FROM mod_calendar_dedup_candidates")).scalar()
    assert n == 0


def test_dedup_candidate_cascade_on_user_delete(seed_user2: int) -> None:
    a = _seed_event(seed_user2)
    b = _seed_event(seed_user2)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_calendar_dedup_candidates "
                "(user_id, event_a_id, event_b_id, reason) VALUES (:u, :a, :b, 'time+title')"
            ),
            {"u": seed_user2, "a": min(a, b), "b": max(a, b)},
        )
        c.execute(text("DELETE FROM users WHERE id = :u"), {"u": seed_user2})
        n = c.execute(
            text("SELECT count(*) FROM mod_calendar_dedup_candidates WHERE user_id = :u"),
            {"u": seed_user2},
        ).scalar()
    assert n == 0
