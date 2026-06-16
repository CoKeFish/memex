"""Cola de candidatos a (re)evaluar — la arman PROCEDIMIENTOS deterministas (ver `procedures.py`).

Este módulo es la cara de lectura/acción de la cola `relevance_candidates`: listar, mover estado y
RE-EVALUAR un candidato por el MOTOR ÚNICO (el juez del gate + los intereses), no un segundo juez
advisory. La detección (qué entra a la cola) vive en `relevance.procedures`;
`run_relevance_detection` acá la delega para conservar el contrato del job `relevance`.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from memex.db import connection
from memex.llm import LLMClient
from memex.logging import get_logger
from memex.relevance.gate import run_relevance_gate
from memex.relevance.procedures import RelevanceDetectStats, run_candidate_detection

_log = get_logger("memex.relevance.candidates")

VALID_STATUS: frozenset[str] = frozenset({"open", "confirmed", "dismissed"})

_CANDIDATE_COLS = (
    "procedure, unit_type, sender_key, sender_label, email, messages, relevant, inert, "
    "relevance_pct, score, status, snapshot, created_at, updated_at"
)


def run_relevance_detection(user_id: int) -> RelevanceDetectStats:
    """Job del scheduler `relevance`: corre los procedimientos y refresca la cola. Sin LLM."""
    return run_candidate_detection(user_id)


def list_candidates(
    conn: Connection,
    *,
    user_id: int,
    status: str | None = "open",
    procedure: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Candidatos del user (rankeados por score = ruido), filtrables por estado y procedimiento."""
    where = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if status is not None:
        where.append("status = :st")
        params["st"] = status
    if procedure is not None:
        where.append("procedure = :proc")
        params["proc"] = procedure
    rows = (
        conn.execute(
            text(
                f"SELECT {_CANDIDATE_COLS} FROM relevance_candidates "
                f"WHERE {' AND '.join(where)} ORDER BY score DESC, messages DESC LIMIT :limit"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


class InvalidStatusError(ValueError):
    """Estado fuera de `VALID_STATUS`."""


def set_candidate_status(
    conn: Connection,
    *,
    user_id: int,
    sender_key: str,
    status: str,
    procedure: str | None = None,
) -> dict[str, Any] | None:
    """Mueve el estado de un candidato (open/confirmed/dismissed). None si no existe.

    Sin `procedure` afecta las filas del remitente en TODOS los procedimientos (devuelve una).
    """
    if status not in VALID_STATUS:
        raise InvalidStatusError(f"estado inválido: {status!r}; válidos: {sorted(VALID_STATUS)}")
    clause = "user_id = :uid AND sender_key = :key"
    params: dict[str, Any] = {"st": status, "uid": user_id, "key": sender_key}
    if procedure is not None:
        clause += " AND procedure = :proc"
        params["proc"] = procedure
    row = (
        conn.execute(
            text(
                f"UPDATE relevance_candidates SET status = :st, updated_at = NOW() "
                f"WHERE {clause} RETURNING {_CANDIDATE_COLS}"
            ),
            params,
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def reevaluate_candidate(
    user_id: int,
    *,
    sender_key: str,
    procedure: str | None = None,
    client: LLMClient | None = None,
) -> dict[str, int] | None:
    """Re-evalúa un candidato por el MOTOR ÚNICO: corre el gate sobre su muestra (force) y devuelve
    el conteo de veredictos. None si el candidato no existe o no tiene muestra.

    Usa `run_relevance_gate`, así que el juez/intereses/proveedor son los del gate (un solo motor).
    Si el gate está apagado, el gate es no-op (no hay juez configurado) y devuelve ceros.
    """
    with connection() as conn:
        where = "user_id = :uid AND sender_key = :key"
        params: dict[str, Any] = {"uid": user_id, "key": sender_key}
        if procedure is not None:
            where += " AND procedure = :proc"
            params["proc"] = procedure
        row = (
            conn.execute(
                text(f"SELECT snapshot FROM relevance_candidates WHERE {where} LIMIT 1"),
                params,
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    sample = [int(i) for i in (row["snapshot"] or {}).get("sample_inbox_ids", [])]
    if not sample:
        return None
    stats = await run_relevance_gate(user_id, inbox_ids=sample, force=True, client=client)
    return {
        "messages": stats.messages,
        "relevant": stats.relevant,
        "not_relevant": stats.not_relevant,
        "insufficient": stats.insufficient,
    }
