"""Cola de candidatos a filtrar — detección automática "por métricas" (determinista, sin LLM).

El job `relevance` (apagado por default) computa la métrica de relevancia por remitente
(`quality.relevance.senders_by_relevance`) y marca como CANDIDATO a cada remitente EMAIL con
suficiente volumen y poca relevancia que AÚN no fue accionado (sin override). Persiste la cola en
`relevance_candidates` (status open/confirmed/dismissed) con una foto del metric + ids de muestra
para validar antes de confirmar. NUNCA auto-aplica: la acción la confirma el humano (Fase 3). Es el
"analyzer costo-vs-valor" diferido, en versión asistida. SQL puro.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from memex.config import settings
from memex.db import connection
from memex.logging import get_logger
from memex.quality.relevance import senders_by_relevance

_log = get_logger("memex.quality.candidates")

VALID_STATUS: frozenset[str] = frozenset({"open", "confirmed", "dismissed"})

_CANDIDATE_COLS = (
    "sender_key, sender_label, email, messages, relevant, inert, relevance_pct, "
    "score, status, snapshot, llm_verdict, created_at, updated_at"
)


@dataclass
class RelevanceDetectStats:
    """Roll-up de una corrida de detección."""

    scanned: int = 0  # remitentes evaluados
    candidates: int = 0  # candidatos upserted (open nuevos o refrescados)


def _sample_inbox_ids(conn: Connection, user_id: int, email: str, limit: int = 3) -> list[int]:
    """Hasta `limit` inbox_ids recientes del remitente — para validar antes de actuar."""
    rows = conn.execute(
        text(
            "SELECT id FROM inbox WHERE user_id = :uid "
            "AND lower(payload->'from'->>'email') = :email "
            "ORDER BY occurred_at DESC LIMIT :lim"
        ),
        {"uid": user_id, "email": email, "lim": limit},
    ).all()
    return [int(r[0]) for r in rows]


def detect_candidates(
    conn: Connection,
    *,
    user_id: int,
    min_messages: int,
    max_relevance_pct: float,
    limit: int = 2000,
) -> RelevanceDetectStats:
    """Upsert de candidatos desde la métrica. Solo remitentes EMAIL con volumen >= `min_messages`,
    `% relevancia` <= `max_relevance_pct` y SIN override (los ya accionados no son candidatos). El
    upsert refresca métricas/snapshot pero NO toca `status` (un descartado no re-abre)."""
    senders = senders_by_relevance(conn, user_id=user_id, limit=limit)
    stats = RelevanceDetectStats(scanned=len(senders))
    for s in senders:
        email = s["email"]
        if email is None or s["override_tier"] is not None:
            continue
        if s["messages"] < min_messages:
            continue
        pct = float(s["relevance_pct"]) if s["relevance_pct"] is not None else 0.0
        if pct > max_relevance_pct:
            continue
        snapshot = {
            "summarized_only": s["summarized_only"],
            "inert": s["inert"],
            "tier_mix": s["tier_mix"],
            "sample_inbox_ids": _sample_inbox_ids(conn, user_id, email),
        }
        conn.execute(
            text(
                """
                INSERT INTO relevance_candidates
                    (user_id, sender_key, sender_label, email, messages, relevant, inert,
                     relevance_pct, score, snapshot)
                VALUES (:uid, :key, :label, :email, :msgs, :rel, :inert, :pct, :score,
                        CAST(:snap AS JSONB))
                ON CONFLICT (user_id, sender_key) DO UPDATE SET
                    sender_label = EXCLUDED.sender_label, email = EXCLUDED.email,
                    messages = EXCLUDED.messages, relevant = EXCLUDED.relevant,
                    inert = EXCLUDED.inert, relevance_pct = EXCLUDED.relevance_pct,
                    score = EXCLUDED.score, snapshot = EXCLUDED.snapshot, updated_at = NOW()
                """
            ),
            {
                "uid": user_id,
                "key": s["sender_key"],
                "label": s["sender_label"],
                "email": email,
                "msgs": s["messages"],
                "rel": s["relevant"],
                "inert": s["inert"],
                "pct": s["relevance_pct"],
                "score": s["inert"],
                "snap": json.dumps(snapshot),
            },
        )
        stats.candidates += 1
    return stats


def run_relevance_detection(user_id: int) -> RelevanceDetectStats:
    """Job del scheduler: llena/refresca la cola de candidatos desde la métrica. Sin LLM."""
    with connection() as conn:
        stats = detect_candidates(
            conn,
            user_id=user_id,
            min_messages=settings.quality_min_messages,
            max_relevance_pct=settings.quality_max_relevance_pct,
        )
    _log.info(
        "quality.relevance.detect",
        user_id=user_id,
        scanned=stats.scanned,
        candidates=stats.candidates,
    )
    return stats


def list_candidates(
    conn: Connection, *, user_id: int, status: str | None = "open", limit: int = 200
) -> list[dict[str, Any]]:
    """Candidatos del user (rankeados por score = ruido), opcionalmente filtrados por estado."""
    where = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    if status is not None:
        where.append("status = :st")
        params["st"] = status
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
    conn: Connection, *, user_id: int, sender_key: str, status: str
) -> dict[str, Any] | None:
    """Mueve el estado de un candidato (open/confirmed/dismissed). None si no existe."""
    if status not in VALID_STATUS:
        raise InvalidStatusError(f"estado inválido: {status!r}; válidos: {sorted(VALID_STATUS)}")
    row = (
        conn.execute(
            text(
                f"UPDATE relevance_candidates SET status = :st, updated_at = NOW() "
                f"WHERE user_id = :uid AND sender_key = :key RETURNING {_CANDIDATE_COLS}"
            ),
            {"st": status, "uid": user_id, "key": sender_key},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None
