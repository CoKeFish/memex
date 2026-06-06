"""`register()` determinista: inserta filas, normaliza categoría, resuelve fecha, sin dedup."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.modules.bienestar.module import register

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def test_register_inserts_row(conn: Connection) -> None:
    row = register(conn, 1, category="comida", activity="Almuerzo", description="  pizza  ")
    assert row["category"] == "comida"
    assert row["activity"] == "Almuerzo"  # normaliza whitespace, conserva mayúsculas
    assert row["description"] == "pizza"  # .strip()
    assert row["occurred_at_precision"] == "datetime"
    assert conn.execute(text("SELECT count(*) FROM mod_bienestar_registros")).scalar_one() == 1


def test_register_defaults_occurred_at_now(conn: Connection) -> None:
    before = datetime.now(UTC)
    row = register(conn, 1, category="ejercicio", activity="gimnasio")
    assert row["occurred_at"] >= before
    assert row["occurred_at_precision"] == "datetime"


def test_register_explicit_datetime(conn: Connection) -> None:
    when = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)
    row = register(
        conn, 1, category="comida", activity="cena", occurred_at=when, precision="datetime"
    )
    assert row["occurred_at"] == when
    assert row["occurred_at_precision"] == "datetime"


def test_register_date_precision(conn: Connection) -> None:
    when = datetime(2026, 6, 5, 0, 0, tzinfo=UTC)
    row = register(
        conn, 1, category="grooming", activity="corte de pelo", occurred_at=when, precision="date"
    )
    assert row["occurred_at_precision"] == "date"


def test_invalid_category_falls_back_to_otros(conn: Connection) -> None:
    row = register(conn, 1, category="inventada", activity="x")
    assert row["category"] == "otros"


def test_register_stores_detail_and_metadata(conn: Connection) -> None:
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
    register(conn, 1, category="higiene", activity="cepillado")
    register(conn, 1, category="higiene", activity="cepillado")
    assert conn.execute(text("SELECT count(*) FROM mod_bienestar_registros")).scalar_one() == 2
