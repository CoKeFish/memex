"""Procedimientos enchufables que ARMAN la lista de candidatos a (re)evaluar.

El ex-«juez advisory» del sistema de calidad se reemplaza por esto: reglas DETERMINISTAS que
eligen QUÉ remitentes (a futuro: tópicos/grupos/clases-de-post) vale la pena (re)evaluar. El
MOTOR ÚNICO —el juez del gate + la MISMA lista de intereses— los evalúa después
(`quality.candidates.reevaluate_candidate`), no un segundo prompt. Sumar un procedimiento = una
clase nueva + una entrada en `CANDIDATE_PROCEDURES`, sin tocar el motor.

Category-agnostic: cada `Candidate` declara su `unit_type` (correo = 'sender'). Hoy el único
procedimiento es `fact_count` — señal núcleo: ¿el remitente produjo HECHOS de dominio?
(`module_extractions.item_count`). «Procesado pero sin hecho» ≠ «sin valor»: no se corta solo,
cae a la cola para que el motor único lo re-evalúe contra los intereses.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import Connection, text

from memex.config import settings
from memex.db import connection
from memex.logging import get_logger
from memex.quality.relevance import senders_by_relevance

_log = get_logger("memex.quality.procedures")


@dataclass(frozen=True)
class Candidate:
    """Un candidato a (re)evaluar propuesto por un procedimiento.

    `unit_type`/`unit_key` son el seam por-ingestor: correo = ('sender', sender_key). `signal`
    es la foto de métricas del procedimiento; `score` ordena la cola (más ruido primero).
    """

    unit_type: str
    unit_key: str
    unit_label: str
    email: str | None
    sample_inbox_ids: list[int]
    signal: dict[str, Any]
    reason: str
    score: int


@dataclass
class RelevanceDetectStats:
    """Roll-up de una corrida de detección (sobre todos los procedimientos)."""

    candidates: int = 0
    procedures: int = 0


class CandidateProcedure(Protocol):
    """Contrato de un procedimiento que arma candidatos. Determinista, sin LLM."""

    name: str

    def detect(self, conn: Connection, *, user_id: int) -> Iterable[Candidate]: ...


def _sample_inbox_ids(conn: Connection, user_id: int, email: str, limit: int = 3) -> list[int]:
    """Hasta `limit` inbox_ids recientes del remitente — la muestra que re-evalúa el motor."""
    rows = conn.execute(
        text(
            "SELECT id FROM inbox WHERE user_id = :uid "
            "AND lower(payload->'from'->>'email') = :email "
            "ORDER BY occurred_at DESC LIMIT :lim"
        ),
        {"uid": user_id, "email": email, "lim": limit},
    ).all()
    return [int(r[0]) for r in rows]


class FactCountProcedure:
    """Remitentes EMAIL con volumen y poca relevancia (señal: ¿produjeron HECHOS? `item_count`).

    Un remitente con >= `min_messages` correos y `% relevancia` <= `max_relevance_pct` que AÚN no
    fue accionado (sin override de tier) entra a la cola. NO se corta solo: el motor único lo
    re-evalúa contra los intereses o el humano confirma. Las perillas son LA calibración
    (`quality_min_messages`/`quality_max_relevance_pct`), sin umbral hardcodeado.
    """

    name = "fact_count"

    def __init__(
        self, *, min_messages: int | None = None, max_relevance_pct: float | None = None
    ) -> None:
        self.min_messages = (
            min_messages if min_messages is not None else settings.quality_min_messages
        )
        self.max_relevance_pct = (
            max_relevance_pct
            if max_relevance_pct is not None
            else settings.quality_max_relevance_pct
        )

    def detect(self, conn: Connection, *, user_id: int) -> Iterable[Candidate]:
        for s in senders_by_relevance(conn, user_id=user_id, limit=2000):
            email = s["email"]
            if email is None or s["override_tier"] is not None:
                continue
            if s["messages"] < self.min_messages:
                continue
            pct = float(s["relevance_pct"]) if s["relevance_pct"] is not None else 0.0
            if pct > self.max_relevance_pct:
                continue
            yield Candidate(
                unit_type="sender",
                unit_key=s["sender_key"],
                unit_label=s["sender_label"],
                email=email,
                sample_inbox_ids=_sample_inbox_ids(conn, user_id, email),
                signal={
                    "messages": int(s["messages"]),
                    "relevant": int(s["relevant"]),
                    "inert": int(s["inert"]),
                    "summarized_only": int(s["summarized_only"]),
                    "relevance_pct": pct,  # float (la métrica vuelve Decimal/NUMERIC)
                    "tier_mix": s["tier_mix"],
                },
                reason=f"{s['messages']} correos, {pct}% con hecho",
                score=int(s["inert"]),
            )


#: Registro de procedimientos. Sumar uno (volumen, solo-resumen, señal de bulk, …) = agregarlo
#: acá; el motor único no cambia.
CANDIDATE_PROCEDURES: dict[str, CandidateProcedure] = {p.name: p for p in (FactCountProcedure(),)}


def _upsert_candidate(conn: Connection, user_id: int, procedure: str, cand: Candidate) -> None:
    """Upsert de un candidato (por procedimiento). Refresca métricas/snapshot, NO toca `status`."""
    snapshot = {**cand.signal, "sample_inbox_ids": cand.sample_inbox_ids, "reason": cand.reason}
    conn.execute(
        text(
            """
            INSERT INTO relevance_candidates
                (user_id, procedure, unit_type, sender_key, sender_label, email,
                 messages, relevant, inert, relevance_pct, score, snapshot)
            VALUES (:uid, :proc, :ut, :key, :label, :email, :msgs, :rel, :inert, :pct, :score,
                    CAST(:snap AS JSONB))
            ON CONFLICT (user_id, procedure, sender_key) DO UPDATE SET
                unit_type = EXCLUDED.unit_type, sender_label = EXCLUDED.sender_label,
                email = EXCLUDED.email, messages = EXCLUDED.messages,
                relevant = EXCLUDED.relevant, inert = EXCLUDED.inert,
                relevance_pct = EXCLUDED.relevance_pct, score = EXCLUDED.score,
                snapshot = EXCLUDED.snapshot, updated_at = NOW()
            """
        ),
        {
            "uid": user_id,
            "proc": procedure,
            "ut": cand.unit_type,
            "key": cand.unit_key,
            "label": cand.unit_label,
            "email": cand.email,
            "msgs": int(cand.signal.get("messages", 0)),
            "rel": int(cand.signal.get("relevant", 0)),
            "inert": int(cand.signal.get("inert", 0)),
            "pct": cand.signal.get("relevance_pct"),
            "score": cand.score,
            "snap": json.dumps(snapshot),
        },
    )


def run_candidate_detection(
    user_id: int, *, procedures: list[str] | None = None
) -> RelevanceDetectStats:
    """Corre los procedimientos (todos por default) y upserta sus candidatos. Sin LLM.

    Cada procedimiento marca de forma independiente (UNIQUE user_id+procedure+sender_key): un
    remitente puede aparecer por más de un procedimiento.
    """
    names = procedures if procedures is not None else list(CANDIDATE_PROCEDURES)
    stats = RelevanceDetectStats()
    with connection() as conn:
        for name in names:
            proc = CANDIDATE_PROCEDURES.get(name)
            if proc is None:
                _log.warning("relevance.procedures.unknown", procedure=name)
                continue
            stats.procedures += 1
            for cand in proc.detect(conn, user_id=user_id):
                _upsert_candidate(conn, user_id, name, cand)
                stats.candidates += 1
    _log.info(
        "relevance.candidates.detect",
        user_id=user_id,
        procedures=stats.procedures,
        candidates=stats.candidates,
    )
    return stats
