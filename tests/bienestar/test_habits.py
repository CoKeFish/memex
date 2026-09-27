"""Hábitos + adherencia: add/list/delete y la racha (gracia, daily/weekly, activity/category)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from memex.modules.bienestar.habits import add_habit, adherence, delete_habit, list_habits
from memex.modules.bienestar.module import register

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_TZ = "America/Bogota"
#: now fijo para tests deterministas: 2026-06-10 12:00 Bogota (UTC-5) = 17:00 UTC.
_NOW = datetime(2026, 6, 10, 17, 0, tzinfo=UTC)


def _reg(conn: Connection, activity: str, day: int, category: str = "higiene") -> None:
    # 13:00 UTC = 08:00 Bogota → cae en la fecha local `day`.
    register(
        conn,
        1,
        category=category,
        activity=activity,
        occurred_at=datetime(2026, 6, day, 13, 0, tzinfo=UTC),
        precision="datetime",
    )


def test_add_list_delete(conn: Connection) -> None:
    h = add_habit(conn, 1, name="Cepillado", cadence="daily", target_count=2, activity="cepillado")
    assert h["cadence"] == "daily"
    assert h["target_count"] == 2
    assert h["activity"] == "cepillado"
    assert len(list_habits(conn, 1)) == 1
    assert delete_habit(conn, 1, int(h["id"])) is True
    assert list_habits(conn, 1) == []


def test_add_requires_activity_or_category(conn: Connection) -> None:
    with pytest.raises(ValueError):
        add_habit(conn, 1, name="x", cadence="daily")


def test_add_rejects_bad_cadence(conn: Connection) -> None:
    with pytest.raises(ValueError):
        add_habit(conn, 1, name="x", cadence="monthly", activity="y")


def test_daily_streak_with_grace(conn: Connection) -> None:
    add_habit(conn, 1, name="Cepillado", cadence="daily", activity="cepillado")
    for d in (8, 9, 10):
        _reg(conn, "cepillado", d)
    a = adherence(conn, 1, tz=_TZ, now=_NOW)[0]
    assert a["current"] == 1
    assert a["met_current"] is True
    assert a["streak"] == 3


def test_daily_grace_today_pending(conn: Connection) -> None:
    # ayer y anteayer cumplidos, hoy todavía no → racha 2 (la gracia no rompe hoy).
    add_habit(conn, 1, name="Cepillado", cadence="daily", activity="cepillado")
    for d in (8, 9):
        _reg(conn, "cepillado", d)
    a = adherence(conn, 1, tz=_TZ, now=_NOW)[0]
    assert a["current"] == 0
    assert a["met_current"] is False
    assert a["streak"] == 2


def test_daily_streak_breaks_on_missed_past(conn: Connection) -> None:
    # 6/10 y 6/8 cumplidos, 6/9 fallado → racha 1 (hoy cuenta; el día fallado corta).
    add_habit(conn, 1, name="Cepillado", cadence="daily", activity="cepillado")
    for d in (8, 10):
        _reg(conn, "cepillado", d)
    a = adherence(conn, 1, tz=_TZ, now=_NOW)[0]
    assert a["streak"] == 1


def test_daily_target_not_met(conn: Connection) -> None:
    add_habit(conn, 1, name="Cepillado", cadence="daily", target_count=2, activity="cepillado")
    _reg(conn, "cepillado", 10)
    a = adherence(conn, 1, tz=_TZ, now=_NOW)[0]
    assert a["current"] == 1
    assert a["met_current"] is False
    assert a["streak"] == 0


def test_match_by_category(conn: Connection) -> None:
    add_habit(conn, 1, name="Moverme", cadence="daily", category="ejercicio")
    _reg(conn, "gimnasio", 10, category="ejercicio")
    a = adherence(conn, 1, tz=_TZ, now=_NOW)[0]
    assert a["current"] == 1
    assert a["met_current"] is True


def test_activity_match_normalized(conn: Connection) -> None:
    add_habit(conn, 1, name="Gym", cadence="daily", activity="gimnasio")
    _reg(conn, "Gimnasio", 10, category="ejercicio")  # mayúsculas distintas
    a = adherence(conn, 1, tz=_TZ, now=_NOW)[0]
    assert a["current"] == 1


def test_weekly_streak(conn: Connection) -> None:
    add_habit(conn, 1, name="Compras", cadence="weekly", activity="compras")
    _reg(conn, "compras", 10)  # esta semana
    _reg(conn, "compras", 3)  # semana anterior (7 días antes → misma weekday)
    a = adherence(conn, 1, tz=_TZ, now=_NOW, periods=4)[0]
    assert a["met_current"] is True
    assert a["streak"] == 2
