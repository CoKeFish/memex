"""`list_registros` + `summary` deterministas: filtros (período/categoría/actividad) y agregados."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from memex.modules.bienestar.module import list_registros, register, summary

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _seed(conn: Connection) -> None:
    register(
        conn, 1, category="comida", activity="almuerzo", occurred_at=_BASE, precision="datetime"
    )
    register(
        conn,
        1,
        category="comida",
        activity="cena",
        occurred_at=_BASE + timedelta(days=1),
        precision="datetime",
    )
    register(
        conn,
        1,
        category="ejercicio",
        activity="gimnasio",
        occurred_at=_BASE + timedelta(days=2),
        precision="datetime",
    )


def test_list_orders_desc_and_limits(conn: Connection) -> None:
    _seed(conn)
    rows = list_registros(conn, 1, limit=2)
    assert len(rows) == 2
    assert rows[0]["activity"] == "gimnasio"  # más nuevo primero


def test_list_filters_by_category(conn: Connection) -> None:
    _seed(conn)
    rows = list_registros(conn, 1, category="comida")
    assert len(rows) == 2
    assert all(r["category"] == "comida" for r in rows)


def test_list_filters_by_activity_normalized(conn: Connection) -> None:
    register(conn, 1, category="ejercicio", activity="Gimnasio")
    rows = list_registros(conn, 1, activity="  gimnasio ")  # insensible a mayúsculas/espacios
    assert len(rows) == 1
    assert rows[0]["activity"] == "Gimnasio"


def test_list_filters_by_period(conn: Connection) -> None:
    _seed(conn)
    since = datetime(2026, 6, 2, 0, 0, tzinfo=UTC)
    rows = list_registros(conn, 1, since=since)
    assert len(rows) == 2  # cena (6/2) y gimnasio (6/3); almuerzo (6/1) queda fuera


def test_summary_counts(conn: Connection) -> None:
    _seed(conn)
    s = summary(conn, 1)
    assert s["total"] == 3
    assert s["by_category"] == {"comida": 2, "ejercicio": 1}
    assert s["by_activity"]["almuerzo"] == 1
    assert s["by_activity"]["gimnasio"] == 1


def test_summary_empty(conn: Connection) -> None:
    s = summary(conn, 1)
    assert s["total"] == 0
    assert s["by_category"] == {}
    assert s["by_activity"] == {}
