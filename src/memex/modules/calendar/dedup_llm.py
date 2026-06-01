"""Dedup FASE 2 (ADR-015 §4): desambiguación LLM de los pares candidatos de la FASE 1.

La FASE 1 (`dedup.py`, determinista) marca pares que PODRÍAN ser el mismo evento. La FASE 2 le
pregunta al LLM, par por par, si realmente lo son, con **sesgo a coexistir**: ante la duda NO se
fusionan (un falso "no son el mismo" deja dos copias —recuperable—; un falso "sí" pierde un
evento). El resultado pasa cada par de `candidate` a `confirmed`/`rejected` con su razón y
confianza; los `confirmed` alimentan la consolidación (slice 3).

Best-effort por par + idempotente: solo procesa `status='candidate'`, así re-correr no re-llama.
Cada llamada se registra en `llm_calls` (`purpose="calendar_dedup"`). Cliente LLM inyectable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, time

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.db import connection
from memex.llm import ChatMessage, DeepSeekClient, LLMClient, LLMConfig, LLMResult
from memex.logging import get_logger
from memex.modules.calendar.prompt import CALENDAR_DEDUP_SYSTEM_PROMPT

_log = get_logger("memex.modules.calendar.dedup_llm")

_DEFAULT_LIMIT = 200
_MAX_TOKENS = 256


@dataclass(frozen=True)
class DedupDecision:
    """Veredicto del LLM sobre un par: ¿son el mismo evento real?"""

    same: bool
    confidence: float
    rationale: str


@dataclass(frozen=True)
class PairEventView:
    """Vista mínima de un evento para mostrarle al LLM (sin ids internos sensibles)."""

    title: str
    starts_on: date
    ends_on: date | None
    start_time: time | None
    end_time: time | None
    location: str
    description: str


@dataclass
class DedupPhase2Stats:
    """Resumen de una corrida de la FASE 2."""

    pairs: int = 0
    confirmed: int = 0
    rejected: int = 0
    errors: int = 0
    #: Costo LLM acumulado. Calendar cruza varios sources → todo va sin source; el total
    #: bajo `calendar.dedup2.end` ya atribuye el costo a calendar (visible, no perdido).
    cost: CostAccum = field(default_factory=CostAccum)


def _fmt_event(label: str, e: PairEventView) -> str:
    fecha = e.starts_on.isoformat()
    if e.ends_on is not None and e.ends_on != e.starts_on:
        fecha += f"..{e.ends_on.isoformat()}"
    if e.start_time is not None:
        hora = e.start_time.strftime("%H:%M")
        if e.end_time is not None:
            hora += f"-{e.end_time.strftime('%H:%M')}"
    else:
        hora = "sin hora"
    lugar = e.location or "(sin lugar)"
    desc = f", detalle={e.description!r}" if e.description else ""
    return f"{label}: titulo={e.title!r}, fecha={fecha}, hora={hora}, lugar={lugar!r}{desc}"


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


async def disambiguate_pair(
    llm: LLMClient, a: PairEventView, b: PairEventView
) -> tuple[DedupDecision, LLMResult]:
    """Le pregunta al LLM si A y B son el mismo evento. Devuelve la decisión + el LLMResult (para
    registrar el costo). Sesgo a coexistir aplicado en el parseo."""
    user_content = (
        "¿Estos dos eventos son el MISMO evento de la vida real?\n\n"
        f"{_fmt_event('A', a)}\n{_fmt_event('B', b)}"
    )
    result = await llm.complete(
        [
            ChatMessage("system", CALENDAR_DEDUP_SYSTEM_PROMPT),
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
    a: PairEventView
    b: PairEventView


def _load_candidates(conn: Connection, user_id: int, limit: int) -> list[_Candidate]:
    rows = (
        conn.execute(
            text(
                """
                SELECT c.id AS pair_id,
                       ea.title AS a_title, ea.starts_on AS a_starts, ea.ends_on AS a_ends,
                       ea.start_time AS a_st, ea.end_time AS a_et,
                       ea.location AS a_loc, ea.description AS a_desc,
                       eb.title AS b_title, eb.starts_on AS b_starts, eb.ends_on AS b_ends,
                       eb.start_time AS b_st, eb.end_time AS b_et,
                       eb.location AS b_loc, eb.description AS b_desc
                FROM mod_calendar_dedup_candidates c
                JOIN mod_calendar_events ea ON ea.id = c.event_a_id
                JOIN mod_calendar_events eb ON eb.id = c.event_b_id
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
    out: list[_Candidate] = []
    for r in rows:
        out.append(
            _Candidate(
                pair_id=int(r["pair_id"]),
                a=PairEventView(
                    title=str(r["a_title"]),
                    starts_on=r["a_starts"],
                    ends_on=r["a_ends"],
                    start_time=r["a_st"],
                    end_time=r["a_et"],
                    location=str(r["a_loc"]),
                    description=str(r["a_desc"]),
                ),
                b=PairEventView(
                    title=str(r["b_title"]),
                    starts_on=r["b_starts"],
                    ends_on=r["b_ends"],
                    start_time=r["b_st"],
                    end_time=r["b_et"],
                    location=str(r["b_loc"]),
                    description=str(r["b_desc"]),
                ),
            )
        )
    return out


def _record_decision(conn: Connection, pair_id: int, decision: DedupDecision) -> None:
    conn.execute(
        text(
            """
            UPDATE mod_calendar_dedup_candidates
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
        _log.info("calendar.dedup2.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    _log.info("calendar.dedup2.start", user_id=user_id, pairs=len(candidates))
    try:
        for cand in candidates:
            stats.pairs += 1
            try:
                decision, result = await disambiguate_pair(llm, cand.a, cand.b)
            except Exception as e:  # best-effort: un par fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "calendar.dedup2.pair_failed",
                    pair_id=cand.pair_id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            with connection() as conn:
                _record_decision(conn, cand.pair_id, decision)
            record_llm_call(
                user_id=user_id,
                purpose="calendar_dedup",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                # La decisión cruza varios sources (par A/B de fuentes distintas) → sin
                # source; se identifica por purpose="calendar_dedup".
                source_id=None,
                metadata={
                    "pair_id": cand.pair_id,
                    "same": decision.same,
                    "confidence": decision.confidence,
                },
            )
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
            if decision.same:
                stats.confirmed += 1
            else:
                stats.rejected += 1
    finally:
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()

    _log.info(
        "calendar.dedup2.end",
        user_id=user_id,
        pairs=stats.pairs,
        confirmed=stats.confirmed,
        rejected=stats.rejected,
        errors=stats.errors,
        # Costo total de calendar (todo sin source); el evento calendar.* lo atribuye a calendar.
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
