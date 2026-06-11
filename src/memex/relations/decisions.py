"""Historial de aristas: veredictos (`relation_edge_decisions`) + sources (`relation_edge_sources`).

Capa BI-CAPA del veredicto par-por-par (subsistema `resolve`): el veredicto vigente ES el status
del edge (`resolve_edge`, monótono); acá vive el POR QUÉ — quién decidió (`method`), con qué regla
o cita (`rule`/`quote`), sobre qué mensaje (`inbox_id`) y con qué evidencia (`evidence_sig`). La
señal original (la pista y sus mensajes) nunca se destruye: NELL candidate→promoted, Wikidata
statement+references.

- Decisiones: log append-only. `confirm`/`reject` se insertan SOLO en la misma tx en que
  `resolve_edge` devolvió True (la monotonía garantiza una sola fila terminal por arista).
  `dejar` no transiciona: es el memo "no decidible con ESTA evidencia" — el resolver no re-gasta
  LLM mientras `evidence_sig` no cambie.
- Sources: TODOS los mensajes que generaron la pista (no solo el primero que quedó en
  `evidence='inbox:N'`). Las puebla `_materialize_cooccurrence` en cada build, también para
  aristas terminales (un rechazado que gana evidencia nueva se detecta comparando la sig actual
  contra la de su última decisión).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

# --- Veredictos (CHECK en la DB) ------------------------------------------------------ #
VERDICT_CONFIRM = "confirm"  #: la relación es real → el edge transicionó a confirmed
VERDICT_REJECT = "reject"  #: co-aparición casual → el edge transicionó a rejected
VERDICT_DEJAR = "dejar"  #: no decidible con esta evidencia (memo; el edge sigue pista)
VALID_VERDICTS: frozenset[str] = frozenset({VERDICT_CONFIRM, VERDICT_REJECT, VERDICT_DEJAR})

# --- Métodos (CHECK en la DB) --------------------------------------------------------- #
METHOD_REGLA = "regla"  #: prefiltro determinista (p.ej. rule='recibo', 'redundante')
METHOD_LLM = "llm"  #: zona gris: el LLM leyó el mensaje y citó (quote grounded)
METHOD_PARTIDOR = "partidor"  #: cascada del partidor de cúmulos (rule='cluster:{id}')
METHOD_HUMANO = "humano"  #: el dueño decidió a mano
VALID_METHODS: frozenset[str] = frozenset(
    {METHOD_REGLA, METHOD_LLM, METHOD_PARTIDOR, METHOD_HUMANO}
)


@dataclass(frozen=True)
class EdgeDecision:
    """Una fila de `relation_edge_decisions` (un veredicto del historial)."""

    id: int
    user_id: int
    edge_id: int
    verdict: str
    method: str
    rule: str
    inbox_id: int | None
    quote: str
    confidence: Decimal | None
    evidence_sig: str
    run_id: str | None


def evidence_signature(inbox_ids: Iterable[int]) -> str:
    """sha256 del set de mensajes-evidencia de un par, ordenado — determinista e independiente del
    orden de entrada (mismo molde que `cluster_signature`). Cambia ⇔ la evidencia cambió."""
    raw = ",".join(str(i) for i in sorted(set(inbox_ids)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def record_decision(
    conn: Connection,
    user_id: int,
    edge_id: int,
    *,
    verdict: str,
    method: str,
    rule: str = "",
    inbox_id: int | None = None,
    quote: str = "",
    confidence: Decimal | None = None,
    evidence_sig: str,
    run_id: str | None = None,
) -> int:
    """Inserta un veredicto en el historial (append-only) y devuelve su id.

    NO toca el edge: la transición de status es responsabilidad del caller (`resolve_edge` en la
    misma tx, para `confirm`/`reject`). `ValueError` si `verdict`/`method` no son válidos (espejo
    del CHECK de la DB, falla antes y con mejor mensaje)."""
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict inválido: {verdict!r}")
    if method not in VALID_METHODS:
        raise ValueError(f"method inválido: {method!r}")
    new_id = conn.execute(
        text(
            """
            INSERT INTO relation_edge_decisions
              (user_id, edge_id, verdict, method, rule, inbox_id, quote, confidence,
               evidence_sig, run_id)
            VALUES (:uid, :eid, :v, :m, :r, :ibx, :q, :conf, :sig, :run)
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "eid": edge_id,
            "v": verdict,
            "m": method,
            "r": rule,
            "ibx": inbox_id,
            "q": quote,
            "conf": confidence,
            "sig": evidence_sig,
            "run": run_id,
        },
    ).scalar_one()
    return int(new_id)


def latest_decisions(
    conn: Connection, user_id: int, edge_ids: Sequence[int]
) -> dict[int, EdgeDecision]:
    """La ÚLTIMA decisión de cada arista pedida (las sin historial no aparecen).

    Es la base del memo de `dejar` (¿la sig vigente coincide?) y del reporte de staleness de
    terminales (¿la evidencia creció después del veredicto?)."""
    if not edge_ids:
        return {}
    rows = (
        conn.execute(
            text(
                """
                SELECT DISTINCT ON (edge_id) *
                FROM relation_edge_decisions
                WHERE user_id = :uid AND edge_id = ANY(:ids)
                ORDER BY edge_id, id DESC
                """
            ),
            {"uid": user_id, "ids": sorted(set(edge_ids))},
        )
        .mappings()
        .all()
    )
    return {
        int(r["edge_id"]): EdgeDecision(
            id=int(r["id"]),
            user_id=int(r["user_id"]),
            edge_id=int(r["edge_id"]),
            verdict=str(r["verdict"]),
            method=str(r["method"]),
            rule=str(r["rule"]),
            inbox_id=(int(r["inbox_id"]) if r["inbox_id"] is not None else None),
            quote=str(r["quote"]),
            confidence=r["confidence"],
            evidence_sig=str(r["evidence_sig"]),
            run_id=(str(r["run_id"]) if r["run_id"] is not None else None),
        )
        for r in rows
    }


def add_edge_sources(conn: Connection, edge_id: int, inbox_ids: Iterable[int]) -> int:
    """Liga mensajes a una pista (procedencia acumulada, idempotente por la PK). Devuelve cuántas
    filas NUEVAS entraron. Se llama también sobre aristas terminales: la procedencia sigue
    creciendo aunque el veredicto ya esté tomado (y eso habilita el reporte de conflicto)."""
    ids = sorted(set(inbox_ids))
    if not ids:
        return 0
    inserted = 0
    for ibx in ids:
        inserted += conn.execute(
            text(
                """
                INSERT INTO relation_edge_sources (edge_id, inbox_id)
                VALUES (:eid, :ibx)
                ON CONFLICT (edge_id, inbox_id) DO NOTHING
                """
            ),
            {"eid": edge_id, "ibx": ibx},
        ).rowcount
    return inserted


def edge_sources(conn: Connection, edge_ids: Sequence[int]) -> dict[int, set[int]]:
    """Los mensajes ligados a cada arista pedida (aristas sin filas no aparecen)."""
    if not edge_ids:
        return {}
    rows = conn.execute(
        text(
            """
            SELECT edge_id, inbox_id FROM relation_edge_sources
            WHERE edge_id = ANY(:ids)
            """
        ),
        {"ids": sorted(set(edge_ids))},
    ).all()
    out: dict[int, set[int]] = {}
    for eid, ibx in rows:
        out.setdefault(int(eid), set()).add(int(ibx))
    return out
