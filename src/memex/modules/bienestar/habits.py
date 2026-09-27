"""Hábitos del módulo bienestar + adherencia derivada (determinista, sin LLM).

Un hábito es un compromiso recurrente que el usuario define (por CLI/agente): nombre, qué cuenta
(`activity` normalizada O `category`), cadencia (`daily`/`weekly`) y meta (`target_count`). La
adherencia NO se persiste: se cuentan los registros (`mod_bienestar_registros`) que matchean por
período, en la TZ de display. La racha tiene GRACIA del período en curso (incompleto hoy no la
rompe; un período pasado fallado sí).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.modules.bienestar.schema import normalize_activity, normalize_category
from memex.relations.deterministic import weave_cumple

#: Misma normalización que el match de actividad en `module.py` (lower + colapso de whitespace). La
#: hace SIEMPRE la DB (columna y bind).
_NORM = "lower(btrim(regexp_replace({x}, '\\s+', ' ', 'g')))"

_HABIT_COLS = "id, name, activity, category, cadence, target_count, active, created_at"


def add_habit(
    conn: Connection,
    user_id: int,
    *,
    name: str,
    cadence: str,
    target_count: int = 1,
    activity: str = "",
    category: str | None = None,
) -> dict[str, Any]:
    """Crea un hábito. Necesita `activity` (match) O `category`. `cadence` ∈ daily|weekly. Teje en
    el acto las aristas «cumple» del grafo contra los registros que ya lo satisfacen (el full-sweep
    es respaldo)."""
    if cadence not in ("daily", "weekly"):
        raise ValueError(f"cadence inválida: {cadence!r}")
    act = normalize_activity(activity)
    cat = normalize_category(category) if category else None
    if not act and cat is None:
        raise ValueError("un hábito necesita activity o category")
    row = (
        conn.execute(
            text(
                f"""
                INSERT INTO mod_bienestar_habits
                  (user_id, name, activity, category, cadence, target_count)
                VALUES (:u, :n, :a, :c, :cad, :t)
                RETURNING {_HABIT_COLS}
                """
            ),
            {
                "u": user_id,
                "n": name.strip(),
                "a": act,
                "c": cat,
                "cad": cadence,
                "t": max(1, target_count),
            },
        )
        .mappings()
        .one()
    )
    weave_cumple(conn, user_id, habit_ids=[int(row["id"])])
    return dict(row)


def list_habits(
    conn: Connection, user_id: int, *, include_inactive: bool = False
) -> list[dict[str, Any]]:
    """Hábitos del user (activos, o todos con `include_inactive`)."""
    where = "user_id = :u" + ("" if include_inactive else " AND active")
    rows = (
        conn.execute(
            text(f"SELECT {_HABIT_COLS} FROM mod_bienestar_habits WHERE {where} ORDER BY id"),
            {"u": user_id},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def delete_habit(conn: Connection, user_id: int, habit_id: int) -> bool:
    """Borra un hábito. Devuelve si existía."""
    rc = conn.execute(
        text("DELETE FROM mod_bienestar_habits WHERE user_id = :u AND id = :id"),
        {"u": user_id, "id": habit_id},
    ).rowcount
    return rc > 0


def _period_keys(cadence: str, today: date, n: int) -> list[date]:
    """Las `n` claves de período (oldest→newest) terminando hoy. daily → un día; weekly → el lunes
    de cada semana (coincide con `date_trunc('week')` de Postgres, que arranca el lunes)."""
    if cadence == "weekly":
        monday = today - timedelta(days=today.weekday())
        return [monday - timedelta(weeks=i) for i in range(n - 1, -1, -1)]
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


def _period_counts(
    conn: Connection, user_id: int, habit: dict[str, Any], tz: str, since_key: date
) -> dict[date, int]:
    """Conteo de registros que matchean el hábito, por período en `tz`, desde `since_key`."""
    since = datetime.combine(since_key, time.min, tzinfo=ZoneInfo(tz)).astimezone(UTC)
    if habit["activity"]:
        match = f"{_NORM.format(x='activity')} = {_NORM.format(x=':val')}"
        val = habit["activity"]
    else:
        match = "category = :val"
        val = habit["category"]
    bucket = (
        "date_trunc('week', (occurred_at AT TIME ZONE :tz))::date"
        if habit["cadence"] == "weekly"
        else "(occurred_at AT TIME ZONE :tz)::date"
    )
    rows = conn.execute(
        text(
            f"""
            SELECT {bucket} AS p, count(*) AS n
            FROM mod_bienestar_registros
            WHERE user_id = :u AND occurred_at >= :since AND {match}
            GROUP BY p
            """
        ),
        {"u": user_id, "tz": tz, "since": since, "val": val},
    ).all()
    return {r[0]: int(r[1]) for r in rows}


def _streak(history: list[dict[str, Any]], _target: int) -> int:
    """Racha = corrida de períodos cumplidos terminando en el período actual (si está cumplido) o
    en el anterior (gracia del período en curso). Un período pasado fallado la corta."""
    idx = len(history) - 1
    if idx >= 0 and not history[idx]["met"]:
        idx -= 1  # gracia: el período en curso incompleto no rompe
    streak = 0
    while idx >= 0 and history[idx]["met"]:
        streak += 1
        idx -= 1
    return streak


def adherence(
    conn: Connection,
    user_id: int,
    *,
    tz: str,
    periods: int = 14,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Adherencia de cada hábito activo: progreso del período en curso, racha (con gracia) e
    historia de los últimos `periods` períodos, en la TZ `tz`. `now` inyectable para tests."""
    zone = ZoneInfo(tz)
    ref = (now or datetime.now(UTC)).astimezone(zone)
    today = ref.date()
    out: list[dict[str, Any]] = []
    for h in list_habits(conn, user_id):
        keys = _period_keys(h["cadence"], today, periods)
        counts = _period_counts(conn, user_id, h, tz, keys[0])
        target = int(h["target_count"])
        history = [
            {"period": k.isoformat(), "count": counts.get(k, 0), "met": counts.get(k, 0) >= target}
            for k in keys
        ]
        current = history[-1]
        out.append(
            {
                "habit": h,
                "cadence": h["cadence"],
                "target_count": target,
                "current": current["count"],
                "met_current": current["met"],
                "streak": _streak(history, target),
                "history": history,
            }
        )
    return out
