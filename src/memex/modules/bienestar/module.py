"""`bienestar` — registrador DETERMINISTA de eventos de salud y bienestar (sin LLM).

NO es un módulo de extracción (`InterestModule`): no consume mensajes ingeridos ni usa el LLM. Lo
maneja un AGENTE EXTERNO (p. ej. Hermes) que entiende el lenguaje natural —por Telegram— y llama
a la CLI `memex-bienestar` con campos YA estructurados. memex solo guarda y reporta; es el guardián
de las invariantes del dato (categoría de la lista cerrada), no el que interpreta. Sin dedup: cada
evento auto-reportado es distinto (dos comidas iguales son dos comidas).

Tres operaciones: `register` (escribe un evento), `list_registros` (lista filtrada) y `summary`
(agregado para reportes). Todas deterministas y síncronas.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.bienestar.habits import list_habits
from memex.modules.bienestar.schema import normalize_activity, normalize_category
from memex.relations.deterministic import weave_cumple, weave_event

_log = get_logger("memex.modules.bienestar")

PRECISION_DATETIME = "datetime"
PRECISION_DATE = "date"

#: Columnas públicas que devuelven `register` / `list_registros` (orden estable).
_PUBLIC_COLS = (
    "id, category, activity, occurred_at, occurred_at_precision, description, detail, metadata, "
    "event_id, created_at"
)

#: Normalización de texto para el match de `activity` en los filtros — misma expresión que el dedup
#: de módulos (lower + colapso de whitespace). La hace SIEMPRE la DB (columna y bind).
_NORM = "lower(btrim(regexp_replace({x}, '\\s+', ' ', 'g')))"


def _habit_label(h: Mapping[str, Any]) -> str:
    """Etiqueta de un hábito para mensajes: nombre + clave de match (actividad o categoría)."""
    clave = h["activity"] or f"categoría {h['category']}"
    return f"{h['name']} ({clave})"


class NoMatchingHabitError(ValueError):
    """Se intentó registrar algo que NINGÚN hábito activo cubre. bienestar es PARA HÁBITOS: un
    registro es el cumplimiento de un hábito, así que esto se RECHAZA (no se guarda) y se devuelve
    la lista de hábitos válidos para que el agente use uno o cree el que falta. Subclase de
    `ValueError` para que los callers que ya capturan `ValueError` la muestren igual."""

    def __init__(self, category: str, activity: str, habits: list[dict[str, Any]]) -> None:
        self.category = category
        self.activity = activity
        self.habits = habits
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        que = (
            f"la actividad '{self.activity}'"
            if self.activity
            else f"la categoría '{self.category}'"
        )
        if not self.habits:
            return (
                f"no hay hábitos activos, así que no se puede registrar {que}: "
                "creá uno con `memex-bienestar habit add`."
            )
        validos = "; ".join(_habit_label(h) for h in self.habits)
        return (
            f"no existe un hábito activo para {que}. Hábitos válidos: {validos}. "
            "Usá uno de esos o creá el que falta con `memex-bienestar habit add`."
        )


def _require_matching_habit(conn: Connection, user_id: int, category: str, activity: str) -> None:
    """Verifica que exista un hábito ACTIVO que el registro cumpla — match por actividad normalizada
    o por categoría, el MISMO que la adherencia y `_materialize_cumple`. Si no hay, lanza
    `NoMatchingHabitError` con la lista de hábitos válidos. Solo LEE."""
    match_act = f"activity <> '' AND {_NORM.format(x='activity')} = {_NORM.format(x=':act')}"
    matched = conn.execute(
        text(
            f"""
            SELECT 1 FROM mod_bienestar_habits
            WHERE user_id = :u AND active
              AND (({match_act}) OR (activity = '' AND category = :cat))
            LIMIT 1
            """
        ),
        {"u": user_id, "act": activity, "cat": category},
    ).first()
    if matched is None:
        raise NoMatchingHabitError(category, activity, list_habits(conn, user_id))


def register(
    conn: Connection,
    user_id: int,
    *,
    category: str,
    activity: str = "",
    description: str = "",
    occurred_at: datetime | None = None,
    precision: str | None = None,
    detail: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Inserta un registro determinista y devuelve la fila pública. `category` se normaliza a la
    lista cerrada (fuera de ella → 'otros'). Sin `occurred_at` usa ahora (UTC, precisión
    `datetime`). **Rechaza con `NoMatchingHabitError`** si ningún hábito activo cubre el registro
    (bienestar es para hábitos: un registro es el cumplimiento de un hábito). Si pasa, teje en el
    acto las aristas «cumple» contra los hábitos que satisface; con `event_id`, además las
    «mismo_evento» contra los hechos que ya comparten el evento (el full-sweep es respaldo). NO usa
    LLM ni deduplica."""
    if occurred_at is None:
        occurred_at = datetime.now(UTC)
        precision = PRECISION_DATETIME
    prec = precision if precision in (PRECISION_DATETIME, PRECISION_DATE) else PRECISION_DATETIME
    cat = normalize_category(category)
    act = normalize_activity(activity)
    # bienestar es PARA HÁBITOS: registrar algo que ningún hábito activo cubre se rechaza (no se
    # guarda un registro huérfano) — devuelve la lista de hábitos válidos para elegir o crear.
    _require_matching_habit(conn, user_id, cat, act)
    row = (
        conn.execute(
            text(
                f"""
                INSERT INTO mod_bienestar_registros
                  (user_id, category, activity, occurred_at, occurred_at_precision, description,
                   detail, metadata, event_id)
                VALUES
                  (:uid, :category, :activity, :occurred_at, :precision, :description,
                   CAST(:detail AS JSONB), CAST(:metadata AS JSONB), :event_id)
                RETURNING {_PUBLIC_COLS}
                """
            ),
            {
                "uid": user_id,
                "category": cat,
                "activity": act,
                "occurred_at": occurred_at,
                "precision": prec,
                "description": description.strip(),
                "detail": json.dumps(dict(detail or {})),
                "metadata": json.dumps(dict(metadata or {})),
                "event_id": event_id,
            },
        )
        .mappings()
        .one()
    )
    _log.info(
        "bienestar.registered",
        user_id=user_id,
        registro_id=int(row["id"]),
        category=row["category"],
        activity=row["activity"],
    )
    weave_cumple(conn, user_id, registro_ids=[int(row["id"])])
    if event_id:
        weave_event(conn, user_id, event_id)
    return dict(row)


def list_registros(
    conn: Connection,
    user_id: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    category: str | None = None,
    activity: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Registros del user, filtrados (período `[since, until)`, categoría, actividad), más nuevos
    primero. `activity` matchea normalizado (insensible a mayúsculas/espacios)."""
    clauses = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if since is not None:
        clauses.append("occurred_at >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("occurred_at < :until")
        params["until"] = until
    if category:
        clauses.append("category = :category")
        params["category"] = normalize_category(category)
    if activity:
        clauses.append(f"{_NORM.format(x='activity')} = {_NORM.format(x=':activity')}")
        params["activity"] = activity
    rows = (
        conn.execute(
            text(
                f"SELECT {_PUBLIC_COLS} FROM mod_bienestar_registros "
                f"WHERE {' AND '.join(clauses)} ORDER BY occurred_at DESC, id DESC LIMIT :limit"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def summary(
    conn: Connection,
    user_id: int,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, Any]:
    """Agregado para reportes: total + conteos por categoría y por actividad (top 20) en el período
    `[since, until)`. Sin LLM — el agente externo arma la narrativa con estos números."""
    clauses = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id}
    if since is not None:
        clauses.append("occurred_at >= :since")
        params["since"] = since
    if until is not None:
        clauses.append("occurred_at < :until")
        params["until"] = until
    where = " AND ".join(clauses)
    total = conn.execute(
        text(f"SELECT count(*) FROM mod_bienestar_registros WHERE {where}"), params
    ).scalar_one()
    by_category = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            text(
                f"SELECT category, count(*) FROM mod_bienestar_registros WHERE {where} "
                "GROUP BY category ORDER BY count(*) DESC, category"
            ),
            params,
        ).all()
    }
    by_activity = {
        str(r[0]): int(r[1])
        for r in conn.execute(
            text(
                f"SELECT activity, count(*) FROM mod_bienestar_registros "
                f"WHERE {where} AND activity <> '' GROUP BY activity "
                "ORDER BY count(*) DESC, activity LIMIT 20"
            ),
            params,
        ).all()
    }
    return {
        "total": int(total),
        "by_category": by_category,
        "by_activity": by_activity,
        "since": since,
        "until": until,
    }
