"""Regresión del incidente 2026-06: `forget_inbox_rows` borraba TODA fila con
`source_inbox_ids = []` del user, pero los eventos de proveedor/módulo (y ahora los manuales)
viven legítimamente con el array vacío — un reproceso cualquiera se llevó los 568 eventos
sincronizados de Google. El fix acota el DELETE a las filas que el paso 1 acaba de vaciar.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import text

from memex.db import connection
from memex.modules.dedup import forget_inbox_rows


def _seed_event(
    *,
    user_id: int = 1,
    inbox_ids: list[int] | None = None,
    origin: str = "extraction",
    title: str = "Evento",
) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    """
                    INSERT INTO mod_calendar_events
                      (user_id, source_inbox_ids, title, starts_on, origin)
                    VALUES (:uid, CAST(:ids AS BIGINT[]), :t, :d, :o)
                    RETURNING id
                    """
                ),
                {
                    "uid": user_id,
                    "ids": inbox_ids or [],
                    "t": title,
                    "d": date(2026, 6, 20),
                    "o": origin,
                },
            ).scalar_one()
        )


def _alive(event_id: int) -> bool:
    with connection() as c:
        return (
            c.execute(
                text("SELECT 1 FROM mod_calendar_events WHERE id = :e"), {"e": event_id}
            ).first()
            is not None
        )


def _inbox_ids(event_id: int) -> list[int]:
    with connection() as c:
        row = c.execute(
            text("SELECT source_inbox_ids FROM mod_calendar_events WHERE id = :e"),
            {"e": event_id},
        ).scalar_one()
    return [int(x) for x in row]


def test_forget_only_deletes_rows_it_emptied() -> None:
    # La regresión exacta: provider y module nacen con [] y deben SOBREVIVIR al forget.
    extraction = _seed_event(inbox_ids=[5], origin="extraction")
    provider = _seed_event(inbox_ids=[], origin="provider", title="Clase (Google)")
    module = _seed_event(inbox_ids=[], origin="module", title="Aportado")

    with connection() as c:
        deleted = forget_inbox_rows(c, "mod_calendar_events", user_id=1, inbox_ids=[5])

    assert deleted == 1
    assert not _alive(extraction)
    assert _alive(provider)
    assert _alive(module)


def test_forget_preserves_shared_rows_with_remaining_ids() -> None:
    shared = _seed_event(inbox_ids=[5, 6])

    with connection() as c:
        deleted = forget_inbox_rows(c, "mod_calendar_events", user_id=1, inbox_ids=[5])

    assert deleted == 0
    assert _inbox_ids(shared) == [6]


def test_forget_scoped_to_user(seed_user2: int) -> None:
    # Una fila vacía de OTRO user jamás se toca (ni siquiera estaba en el [] del bug).
    other_empty = _seed_event(user_id=seed_user2, inbox_ids=[], origin="provider")
    mine = _seed_event(inbox_ids=[5])

    with connection() as c:
        deleted = forget_inbox_rows(c, "mod_calendar_events", user_id=1, inbox_ids=[5])

    assert deleted == 1
    assert not _alive(mine)
    assert _alive(other_empty)


def test_forget_no_matches_is_noop() -> None:
    untouched = _seed_event(inbox_ids=[7])
    with connection() as c:
        deleted = forget_inbox_rows(c, "mod_calendar_events", user_id=1, inbox_ids=[999])
    assert deleted == 0
    assert _inbox_ids(untouched) == [7]
