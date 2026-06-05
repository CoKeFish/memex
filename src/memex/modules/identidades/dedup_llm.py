"""Desempate LLM (FASE 2) de los candidatos de merge de identidades.

El dedup inline (`module.dedup`) encola en `mod_identidades_merge_candidates` los pares de la ZONA
GRIS del difuso (similitud de trigramas en `[LOW, HIGH)`). Esta fase le pregunta al LLM, par por
par, si son la MISMA identidad real, con **sesgo a coexistir** (ante la duda NO se fusionan: un
falso "no" deja dos copias —recuperable—; un falso "sí" pierde una identidad). Si confirma →
`merge_identities`
(la superviviente = id menor, la absorbida = id mayor; al borrarse la absorbida el candidato cae por
FK CASCADE). Si rechaza → el candidato pasa a `rejected`.

Best-effort por par + idempotente: solo procesa `status='candidate'`. Cada llamada se registra en
`llm_calls` (`purpose="identidades_dedup"`). Cliente LLM inyectable (tests con fake).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.core.observability import CostAccum, record_llm_call
from memex.core.trace import attach_to_entity
from memex.db import connection
from memex.llm import ChatMessage, DeepSeekClient, LLMClient, LLMConfig, LLMResult
from memex.logging import get_logger
from memex.modules.identidades.merge import merge_identities
from memex.modules.identidades.prompt import IDENTIDADES_DEDUP_SYSTEM_PROMPT

_log = get_logger("memex.modules.identidades.dedup_llm")

_DEFAULT_LIMIT = 200
_MAX_TOKENS = 256


@dataclass(frozen=True)
class MergeDecision:
    """Veredicto del LLM sobre un par: ¿son la misma identidad real?"""

    same: bool
    confidence: float
    rationale: str


@dataclass(frozen=True)
class IdentityView:
    """Vista mínima de una identidad para mostrarle al LLM (sin ids internos)."""

    kind: str
    display_name: str
    aliases: Sequence[str]
    identifiers: Sequence[str]


@dataclass
class MergePhase2Stats:
    """Resumen de una corrida de la FASE 2 del merge de identidades."""

    pairs: int = 0
    merged: int = 0
    rejected: int = 0
    errors: int = 0
    cost: CostAccum = field(default_factory=CostAccum)


def _fmt_identity(label: str, v: IdentityView) -> str:
    alias = ", ".join(v.aliases) if v.aliases else "(sin alias)"
    idf = ", ".join(v.identifiers) if v.identifiers else "(sin identificadores)"
    return f"{label}: tipo={v.kind}, nombre={v.display_name!r}, alias=[{alias}], ids=[{idf}]"


def _parse_decision(content: str) -> MergeDecision:
    """Parsea la respuesta del LLM. Ambigüedad/falla → NO son la misma (sesgo a coexistir)."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return MergeDecision(same=False, confidence=0.0, rationale="parse_fallback")
    if not isinstance(data, dict):
        return MergeDecision(same=False, confidence=0.0, rationale="parse_fallback")
    same = data.get("same")
    if not isinstance(same, bool):
        return MergeDecision(same=False, confidence=0.0, rationale="parse_fallback")
    raw_conf = data.get("confidence")
    confidence = float(raw_conf) if isinstance(raw_conf, (int, float)) else 0.0
    confidence = max(0.0, min(1.0, confidence))
    rationale = str(data.get("rationale") or "")[:500]
    return MergeDecision(same=same, confidence=confidence, rationale=rationale)


async def disambiguate_pair(
    llm: LLMClient, a: IdentityView, b: IdentityView
) -> tuple[MergeDecision, LLMResult]:
    """Le pregunta al LLM si A y B son la misma identidad. Devuelve la decisión + el LLMResult (para
    el costo). Sesgo a coexistir aplicado en el parseo."""
    user_content = (
        "¿Estas dos entradas son la MISMA identidad (persona u organización) del mundo real?\n\n"
        f"{_fmt_identity('A', a)}\n{_fmt_identity('B', b)}"
    )
    result = await llm.complete(
        [
            ChatMessage("system", IDENTIDADES_DEDUP_SYSTEM_PROMPT),
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
    a: IdentityView
    b: IdentityView


def _load_candidates(conn: Connection, user_id: int, limit: int) -> list[_Candidate]:
    rows = (
        conn.execute(
            text(
                """
                SELECT c.id AS pair_id,
                       a.id AS a_id, a.kind AS a_kind, a.display_name AS a_name,
                       a.aliases AS a_aliases,
                       b.id AS b_id, b.kind AS b_kind, b.display_name AS b_name,
                       b.aliases AS b_aliases,
                       (SELECT array_agg(platform || ':' || kind || ':' || value_norm)
                          FROM mod_identidades_identifiers WHERE identity_id = a.id) AS a_idf,
                       (SELECT array_agg(platform || ':' || kind || ':' || value_norm)
                          FROM mod_identidades_identifiers WHERE identity_id = b.id) AS b_idf
                FROM mod_identidades_merge_candidates c
                JOIN mod_identidades a ON a.id = c.identity_a_id
                JOIN mod_identidades b ON b.id = c.identity_b_id
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
                a_id=int(r["a_id"]),
                b_id=int(r["b_id"]),
                a=IdentityView(
                    kind=str(r["a_kind"]),
                    display_name=str(r["a_name"]),
                    aliases=tuple(r["a_aliases"] or ()),
                    identifiers=tuple(r["a_idf"] or ()),
                ),
                b=IdentityView(
                    kind=str(r["b_kind"]),
                    display_name=str(r["b_name"]),
                    aliases=tuple(r["b_aliases"] or ()),
                    identifiers=tuple(r["b_idf"] or ()),
                ),
            )
        )
    return out


def _attach_desempate(
    conn: Connection, user_id: int, cand: _Candidate, call_id: int, decision: MergeDecision
) -> None:
    """Cuelga el desempate LLM (FASE 2) a la entidad de UNA de las dos identidades del par —la que
    tenga nodo de traza— como hoja `llm` con su costo y output crudo. Solo una (no doble-contar el
    costo); no-op si ninguna fue extraída por-mensaje. Tras un merge la entidad absorbida sigue
    referenciada en la traza (ref_id sin FK), así que el lookup igual la encuentra."""
    for ident_id, other in ((cand.a_id, cand.b_id), (cand.b_id, cand.a_id)):
        node = attach_to_entity(conn, user_id=user_id, table="mod_identidades", ref_id=ident_id)
        if node is not None:
            node.llm(
                call_id,
                label=f"desempate LLM · vs #{other}",
                status="ok",
                detail={"same": decision.same, "confidence": round(decision.confidence, 2)},
            )
            return


def _reject(conn: Connection, pair_id: int, decision: MergeDecision) -> None:
    conn.execute(
        text(
            """
            UPDATE mod_identidades_merge_candidates
            SET status = 'rejected', decided_by = 'llm', confidence = :conf,
                rationale = :rat, decided_at = NOW()
            WHERE id = :id AND status = 'candidate'
            """
        ),
        {"id": pair_id, "conf": decision.confidence, "rat": decision.rationale},
    )


async def run_merge_phase2(
    user_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    client: LLMClient | None = None,
) -> MergePhase2Stats:
    """Resuelve los candidatos `candidate` del user con el LLM. Best-effort por par. Idempotente
    (solo toca `candidate`). `client` inyectable (tests con fake)."""
    stats = MergePhase2Stats()
    with connection() as conn:
        candidates = _load_candidates(conn, user_id, limit)
    if not candidates:
        _log.info("identidades.dedup2.empty", user_id=user_id)
        return stats

    owns_client = client is None
    llm: LLMClient = client if client is not None else DeepSeekClient(LLMConfig.from_env())
    _log.info("identidades.dedup2.start", user_id=user_id, pairs=len(candidates))
    try:
        for cand in candidates:
            stats.pairs += 1
            try:
                decision, result = await disambiguate_pair(llm, cand.a, cand.b)
            except Exception as e:  # best-effort: un par fallido no frena los demás
                stats.errors += 1
                _log.error(
                    "identidades.dedup2.pair_failed",
                    pair_id=cand.pair_id,
                    exc_type=type(e).__name__,
                    exc_msg=str(e),
                )
                continue
            survivor, absorbed = sorted((cand.a_id, cand.b_id))
            with connection() as conn:
                if decision.same:
                    # merge: borra la absorbida → el candidato cae por FK CASCADE (audit en
                    # survivor.metadata.merged_from + llm_calls).
                    if merge_identities(conn, user_id, survivor, absorbed):
                        stats.merged += 1
                    else:
                        stats.errors += 1
                else:
                    _reject(conn, cand.pair_id, decision)
                    stats.rejected += 1
            call_id = record_llm_call(
                user_id=user_id,
                purpose="identidades_dedup",
                model=result.model,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                cache_hit_tokens=result.usage.cache_hit_tokens,
                cost_usd=result.cost_usd,
                latency_ms=result.latency_ms,
                status="ok",
                source_id=None,
                metadata={
                    "pair_id": cand.pair_id,
                    "same": decision.same,
                    "confidence": decision.confidence,
                },
            )
            # Traza: cuelga el desempate a la entidad de una de las identidades (best-effort).
            with connection() as conn:
                _attach_desempate(conn, user_id, cand, call_id, decision)
            stats.cost.calls += 1
            stats.cost.prompt_tokens += result.usage.prompt_tokens
            stats.cost.completion_tokens += result.usage.completion_tokens
            stats.cost.cost_usd += result.cost_usd
    finally:
        if owns_client and isinstance(llm, DeepSeekClient):
            await llm.aclose()

    _log.info(
        "identidades.dedup2.end",
        user_id=user_id,
        pairs=stats.pairs,
        merged=stats.merged,
        rejected=stats.rejected,
        errors=stats.errors,
        llm_calls=stats.cost.calls,
        llm_cost_usd=str(stats.cost.cost_usd),
    )
    return stats
