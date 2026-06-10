"""Persistencia de `worker_runs`: una fila por corrida de cada job del scheduler.

Espeja `mod_calendar_sync_runs._start_run/_finish_run` (sync.py) pero genérico para cualquier
job. Cada helper abre su PROPIA `connection()` (auto-commit): la fila 'running' commitea de
inmediato, así un daemon que muere a media corrida deja una fila huérfana VISIBLE (diagnóstico),
no un hueco. `stats` se serializa con `dataclasses.asdict` (o tal cual si ya es dict) → JSONB.
"""

from __future__ import annotations

import dataclasses
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _json_default(obj: Any) -> Any:
    """Las stats de jobs cargan `Decimal` (CostBySource/CostAccum.cost_usd) que `json.dumps` no
    serializa solo; va a float (es un agregado de display, no contabilidad). Resto → str."""
    if isinstance(obj, Decimal):
        return float(obj)
    return str(obj)


def start_run(user_id: int, job: str) -> int:
    """Inserta la fila 'running' de una corrida y devuelve su id."""
    with connection() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO worker_runs (user_id, job, status) "
                    "VALUES (:uid, :job, 'running') RETURNING id"
                ),
                {"uid": user_id, "job": job},
            ).scalar_one()
        )


def finish_run(run_id: int, *, status: str, stats: Any = None, error: str | None = None) -> None:
    """Cierra la fila: status final, stats (dataclass o dict → JSON) y error opcional.

    Acepta tanto un dataclass (corridas del daemon, p. ej. `RunStats`) como un `dict` ya armado
    (corridas reprocess del API, donde `reprocess()` devuelve un dict con `results` por etapa).
    """
    payload = "{}"
    if stats is not None and dataclasses.is_dataclass(stats) and not isinstance(stats, type):
        payload = json.dumps(dataclasses.asdict(stats), default=_json_default)
    elif isinstance(stats, dict):
        payload = json.dumps(stats, default=_json_default)
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE worker_runs SET status = :s, stats = CAST(:stats AS JSONB), "
                "error = :err, finished_at = NOW() WHERE id = :id"
            ),
            {"id": run_id, "s": status, "stats": payload, "err": error[:2000] if error else None},
        )
