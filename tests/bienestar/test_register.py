"""`register()` determinista: inserta filas, normaliza categoría, resuelve fecha, sin dedup.

bienestar es PARA HÁBITOS: `register` exige un hábito activo que cubra el registro (match por
actividad o categoría); si no, lo RECHAZA con la lista de hábitos válidos. Por eso cada test siembra
los hábitos que necesita.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from memex.modules.bienestar.habits import add_habit
from memex.modules.bienestar.module import NoMatchingHabitError, register

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_CATEGORIES = ("comida", "higiene", "ejercicio", "grooming", "salud", "otros")


def _seed_habits(conn: Connection) -> None:
    """Un hábito de categoría por cada categoría (registrar exige un hábito que lo cubra)."""
    for cat in _CATEGORIES:
        add_habit(conn, 1, name=cat, cadence="daily", category=cat)


def test_register_inserts_row(conn: Connection) -> None:
    _seed_habits(conn)
    row = register(conn, 1, category="comida", activity="Almuerzo", description="  pizza  ")
    assert row["category"] == "comida"
    assert row["activity"] == "Almuerzo"  # normaliza whitespace, conserva mayúsculas
    assert row["description"] == "pizza"  # .strip()
    assert row["occurred_at_precision"] == "datetime"
    assert conn.execute(text("SELECT count(*) FROM mod_bienestar_registros")).scalar_one() == 1


def test_register_defaults_occurred_at_now(conn: Connection) -> None:
    _seed_habits(conn)
    before = datetime.now(UTC)
    row = register(conn, 1, category="ejercicio", activity="gimnasio")
    assert row["occurred_at"] >= before
    assert row["occurred_at_precision"] == "datetime"


def test_register_explicit_datetime(conn: Connection) -> None:
    _seed_habits(conn)
    when = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)
    row = register(
        conn, 1, category="comida", activity="cena", occurred_at=when, precision="datetime"
    )
    assert row["occurred_at"] == when
    assert row["occurred_at_precision"] == "datetime"


def test_register_date_precision(conn: Connection) -> None:
    _seed_habits(conn)
    when = datetime(2026, 6, 5, 0, 0, tzinfo=UTC)
    row = register(
        conn, 1, category="grooming", activity="corte de pelo", occurred_at=when, precision="date"
    )
    assert row["occurred_at_precision"] == "date"


def test_invalid_category_falls_back_to_otros(conn: Connection) -> None:
    _seed_habits(conn)
    row = register(conn, 1, category="inventada", activity="x")
    assert row["category"] == "otros"


def test_register_stores_detail_and_metadata(conn: Connection) -> None:
    _seed_habits(conn)
    row = register(
        conn,
        1,
        category="comida",
        activity="almuerzo",
        detail={"calories": 800},
        metadata={"source_text": "me comí una pizza"},
    )
    assert row["detail"] == {"calories": 800}
    assert row["metadata"] == {"source_text": "me comí una pizza"}


def test_no_dedup_two_identical(conn: Connection) -> None:
    # dos eventos idénticos = dos filas (sin dedup).
    _seed_habits(conn)
    register(conn, 1, category="higiene", activity="cepillado")
    register(conn, 1, category="higiene", activity="cepillado")
    assert conn.execute(text("SELECT count(*) FROM mod_bienestar_registros")).scalar_one() == 2


def test_register_rejects_without_matching_habit(conn: Connection) -> None:
    # es para hábitos: registrar algo que ningún hábito activo cubre se rechaza (sin guardar).
    with pytest.raises(NoMatchingHabitError) as exc:
        register(conn, 1, category="comida", activity="almuerzo")
    assert exc.value.habits == []  # no hay hábitos → lista vacía
    assert conn.execute(text("SELECT count(*) FROM mod_bienestar_registros")).scalar_one() == 0


def test_register_error_lists_valid_habits(conn: Connection) -> None:
    add_habit(conn, 1, name="Gym", cadence="daily", activity="gimnasio")
    with pytest.raises(NoMatchingHabitError) as exc:
        register(conn, 1, category="comida", activity="pizza")  # no matchea "gimnasio"
    assert "Gym" in [h["name"] for h in exc.value.habits]  # el error trae los hábitos válidos
    assert conn.execute(text("SELECT count(*) FROM mod_bienestar_registros")).scalar_one() == 0
