"""Dedup FASE 2 (ADR-015 §4): desambiguación LLM de los pares candidatos de la FASE 1 de finance.

La FASE 1 (`dedup.py`, determinista) marca pares que PODRÍAN ser el mismo movimiento; los que
pasaron la banda alta CON hora confiable ya quedaron `confirmed` (procedimental, sin LLM). La FASE 2
le pregunta al LLM, par por par, por los que quedaron `candidate`, con **sesgo a coexistir**: ante
la duda NO se fusionan (un falso "no son el mismo" deja dos copias —recuperable—; un falso "sí"
pierde un cargo y descuadra la cuenta). El resultado pasa cada par de `candidate` a
`confirmed`/`rejected`; los `confirmed` (procedimentales + LLM) alimentan la consolidación.

Best-effort por par + idempotente: solo procesa `status='candidate'`, así re-correr no re-llama.
Cada llamada se registra en `llm_calls` (`purpose="finance_dedup"`). Cliente LLM inyectable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.core.trace import attach_to_entity
from memex.db import connection
from memex.llm import ChatMessage, LLMClient, LLMResult, aclose_llm, build_llm_client
from memex.logging import get_logger
from memex.modules.finance import fx
from memex.modules.finance.prompt import FINANCE_DEDUP_SYSTEM_PROMPT

_log = get_logger("memex.modules.finance.dedup_llm")

_DEFAULT_LIMIT = 200
_MAX_TOKENS = 256


@dataclass(frozen=True)
class DedupDecision:
    """Veredicto del LLM sobre un par: ¿son el mismo movimiento real?"""

    same: bool
    confidence: float
    rationale: str


@dataclass(frozen=True)
class PairTxView:
    """Vista mínima de una transacción para mostrarle al LLM (sin ids internos)."""

    direction: str
    amount: Decimal
    currency: str
    category: str
    counterparty: str
    place: str
    occurred_at: datetime
    precision: str
    description: str


@dataclass
class DedupPhase2Stats:
    """Resumen de una corrida de la FASE 2."""

    pairs: int = 0
    confirmed: int = 0
    rejected: int = 0
    errors: int = 0
    #: Costo LLM acumulado. El par cruza varias fuentes → todo va sin source; el total bajo
    #: `finance.dedup2.end` ya atribuye el costo a finance (visible, no perdido).
    cost: CostAccum = field(default_factory=CostAccum)


def _fmt_tx(label: str, t: PairTxView) -> str:
    if t.precision == "datetime":
        cuando = t.occurred_at.strftime("%Y-%m-%d %H:%M")
    elif t.precision == "date":
        cuando = t.occurred_at.strftime("%Y-%m-%d") + " (sin hora)"
    else:
        cuando = "~" + t.occurred_at.strftime("%Y-%m-%d") + " (fecha de recepción)"
    quien = t.counterparty or "(sin contraparte)"
    donde = t.place or "(sin lugar)"
    desc = f", detalle={t.description!r}" if t.description else ""
    return (
        f"{label}: {t.direction} {t.amount} {t.currency}, cuando={cuando}, "
        f"contraparte={quien!r}, lugar={donde!r}, rubro={t.category}{desc}"
    )


def _parse_decision(content: str) -> DedupDecision:
    """Parsea la respuesta del LLM. Ambigüedad/falla → NO son el mismo (sesgo a coexistir)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return DedupDecision(same=False, confidence=0.0, rationale="parse_fallback")
    if not isinstance(data, dict):
        return DedupDecision(same=False, confidence=0.0, rationale="parse_fallback")
    same = data.get("same")
    if not isinstance(same, bool):
        return DedupDecision(same=False, confidence=0.0, rationale="parse_fallback")
    raw_conf = data.get("confidence")
    confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else 0.0
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(data.get("rationale") or "")[:500]
    return DedupDecision(same=same, confidence=confidence, rationale=rationale)


def _fx_hint(a: PairTxView, b: PairTxView) -> str:
    """Pista de conversión cuando A y B están en monedas distintas: B convertido a la moneda de A a
    tasa aproximada (vacío si misma moneda o no hay tasa para alguna)."""
    if a.currency.strip().upper() == b.currency.strip().upper():
        return ""
    converted = fx.convert(b.amount, b.currency, a.currency)
    if converted is None:
        return ""
    return (
        f"\nPISTA de conversión (tasa aproximada): {b.amount} {b.currency} ≈ "
        f"{converted.quantize(Decimal('0.01'))} {a.currency} (la de A). Las tasas varían; tolerá "
        "el spread."
    )


async def disambiguate_pair(
    llm: LLMClient, a: PairTxView, b: PairTxView
) -> tuple[DedupDecision, LLMResult]:
    """Le pregunta al LLM si A y B son el mismo movimiento. Devuelve la decisión + el LLMResult
    (para registrar el costo). Sesgo a coexistir aplicado en el parseo."""
    user_content = (
        "¿Estos dos movimientos son el MISMO cobro/pago de la vida real?\n\n"
        f"{_fmt_tx('A', a)}\n{_fmt_tx('B', b)}{_fx_hint(a, b)}"
    )
    result = await llm.complete(
        [
            ChatMessage("system", FINANCE_DEDUP_SYSTEM_PROMPT),
            ChatMessage("user", user_content),
        ],
        response_format="json_object",
        temperature=0.0,
        max_tokens=_MAX_TOKENS,
    )
    return _parse_decision(result.content), result


# --- worker ------------------------------------------------------------------------ #


@dataclass(frozen=True)
class _Candidate:
    pair_id: int
    a_id: int
    b_id: int
    a: PairTxView
    b: PairTxView


def _view(prefix: str, r: dict[str, object]) -> PairTxView:
    return PairTxView(
        direction=str(r[f"{prefix}_direction"]),
        amount=r[f"{prefix}_amount"],  # type: ignore[arg-type]
        currency=str(r[f"{prefix}_currency"]),
        category=str(r[f"{prefix}_category"]),
        counterparty=str(r[f"{prefix}_counterparty"]),
        place=str(r[f"{prefix}_place"]),
        occurred_at=r[f"{prefix}_occurred_at"],  # type: ignore[arg-type]
        precision=str(r[f"{prefix}_precision"]),
        description=str(r[f"{prefix}_description"]),
    )


def _load_candidates(conn: Connection, user_id: int, limit: int) -> list[_Candidate]:
    rows = (
        conn.execute(
            text(
                """
                SELECT c.id AS pair_id, c.transaction_a_id, c.transaction_b_id,
                       ta.direction AS a_direction, ta.amount AS a_amount,
                       ta.currency AS a_currency, ta.category AS a_category,
                       ta.counterparty AS a_counterparty, ta.place AS a_place,
                       ta.occurred_at AS a_occurred_at,
                       ta.occurred_at_precision AS a_precision, ta.description AS a_description,
                       tb.direction AS b_direction, tb.amount AS b_amount,
                       tb.currency AS b_currency, tb.category AS b_category,
                       tb.counterparty AS b_counterparty, tb.place AS b_place,
                       tb.occurred_at AS b_occurred_at,
                       tb.occurred_at_precision AS b_precision, tb.description AS b_description
                FROM mod_finance_dedup_candidates c
                JOIN mod_finance_transactions ta ON ta.id = c.transaction_a_id
                JOIN mod_finance_transactions tb ON tb.id = c.transaction_b_id
                WHERE c.user_id = :uid AND c.status = 'candidate'
                ORDER BY c.id
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [
        _Candidate(
            pair_id=int(r["pair_id"]),
            a_id=int(r["transaction_a_id"]),
            b_id=int(r["transaction_b_id"]),
            a=_view("a", dict(r)),
            b=_view("b", dict(r)),
        )
        for r in rows
    ]


def _attach_desempate(
    conn: Connection, user_id: int, cand: _Candidate, call_id: int, decision: DedupDecision
) -> None:
    """Cuelga el desempate LLM (FASE 2) a la entidad de UNA de las dos transacciones del par —la que
    tenga nodo de traza— como hoja `llm` con su costo y output crudo. Solo una (no doble-contar el
    costo); no-op si ninguna fue extraída por-mensaje (batch)."""
    for tx_id, other in ((cand.a_id, cand.b_id), (cand.b_id, cand.a_id)):
        node = attach_to_entity(
            conn, user_id=user_id, table="mod_finance_transactions", ref_id=tx_id
        )
        if node is not None:
            node.llm(
                call_id,
                label=f"desempate LLM · vs #{other}",
                status="ok",
                detail={"same": decision.same, "confidence": round(decision.confidence, 2)},
            )
            return


def _record_decision(conn: Connection, pair_id: int, decision: DedupDecision) -> None:
    conn.execute(
        text(
            """
            UPDATE mod_finance_dedup_candidates
            SET status = :status, decided_by = 'llm', confidence = :conf,
                rationale = :rat, decided_at = NOW()
            WHERE id = :id
            """
        ),
        {
            "id": pair_id,
            "status": "confirmed" if decision.same else "rejected",
            "conf": decision.confidence,
            "rat": decision.rationale,
        },
    )


async def run_dedup_phase2(
    user_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    client: LLMClient | None = None,
) -> DedupPhase2Stats:
    """Resuelve los pares `candidate` del user con el LLM. Best-effort por par. Idempotente
    (solo toca `candidate`). `client` inyectable (tests con fake)."""
    stats = DedupPhase2Stats()
    with connection() as conn:
        candidates = _load_candidates(conn, user_id, limit)
    if not candidates:
        _log.info("finance.dedup2.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client or build_llm_client("finance_dedup", user_id=user_id)
    _log.info("finance.dedup2.start", user_id=user_id, pairs=len(candidates))
    try:
        for cand in candidates:
            stats.pairs += 1
            try:
                decision, result = await disambiguate_pair(llm, cand.a, cand.b)
            except Exception as e:  # best-effort: un par fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "finance.dedup2.pair_failed",
                    pair_id=cand.pair_id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            with connection() as conn:
                _record_decision(conn, cand.pair_id, decision)
            call_id = record_llm_call(
                user_id=user_id,
                purpose="finance_dedup",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                # La decisión cruza varias fuentes (par A/B de fuentes distintas) → sin source; se
                # identifica por purpose="finance_dedup".
                source_id=None,
                metadata={
                    "pair_id": cand.pair_id,
                    "same": decision.same,
                    "confidence": decision.confidence,
                },
            )
            # Traza: cuelga el desempate a la entidad de una de las transacciones (best-effort).
            with connection() as conn:
                _attach_desempate(conn, user_id, cand, call_id, decision)
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
            if decision.same:
                stats.confirmed += 1
            else:
                stats.rejected += 1
    finally:
        if owns_client:
            await aclose_llm(llm)

    _log.info(
        "finance.dedup2.end",
        user_id=user_id,
        pairs=stats.pairs,
        confirmed=stats.confirmed,
        rejected=stats.rejected,
        errors=stats.errors,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
