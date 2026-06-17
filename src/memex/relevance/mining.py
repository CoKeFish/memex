"""Minería de reglas: segunda pasada del gate que destila reglas COMPUESTAS de las dos polaridades.

El comportamiento definido por el dueño: al juntarse N+ correos (configurable) de un mismo
remitente con el mismo veredicto, el sistema los manda al LLM para que OTORGUE una regla
determinista — `block` para los NO relevantes (ruido), `allow` para los relevantes (valiosos) +
los rescates manuales del dueño. Un solo motor, parametrizado por `effect`.

Las reglas son COMPUESTAS (remitente + patrón del asunto): el remitente solo es demasiado grueso.
El LLM puede declarar «datos insuficientes» (no propone) si no ve un patrón de asunto claro. Cada
propuesta se valida con un DRY RUN determinista contra el histórico — si una `block` atraparía un
relevante (o una `allow`, un no-relevante), queda `rejected` con su reporte. Si pasa, se
AUTO-ACTIVA (auditada y reversible). La garantía de seguridad NO es el LLM: es el dry run.

Una sola llamada LLM por corrida y polaridad (`purpose="relevance_rules"`, `metadata.effect`):
recibe el AGREGADO por DOMINIO del remitente (conteos + remitentes y asuntos de ejemplo +
list_id), no los correos crudos.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Connection, text

from memex.core.observability import CostBySource, record_llm_call
from memex.db import connection
from memex.llm import AnthropicClient, ChatMessage, LLMClient
from memex.logging import get_logger
from memex.relevance.prompts import (
    build_rules_user_content,
    parse_rule_proposals,
    rules_system_prompt,
)
from memex.relevance.providers import build_gate_client
from memex.relevance.rules import EFFECTS, create_rule, dry_run_rule
from memex.relevance.settings import get_settings
from memex.relevance.verdicts import EMAIL_TYPES

_log = get_logger("memex.relevance.mining")

#: Tope de correos que alimentan el agregado (los más recientes), por polaridad.
_DEFAULT_LIMIT = 500
#: Remitentes de ejemplo por dominio en el agregado.
_SENDER_SAMPLES = 5
#: Asuntos de ejemplo por dominio: holgados para que el LLM vea el patrón recurrente.
_SUBJECT_SAMPLES = 12
_MAX_TOKENS = 2048

#: Correos "fuente" de cada polaridad (los que acumulan hacia una regla):
#: - block: los que el JUEZ marcó no-relevantes (`method='llm'`); los `manual`/`rule` ya están
#:   resueltos y no se re-analizan.
#: - allow: los que el JUEZ marcó relevantes (`method='llm'`) MÁS los rescates manuales del dueño
#:   (`relevance_marks.is_relevant = TRUE`) — el lazo de rescate que motiva la allow-list.
_BLOCK_QUALIFYING_SQL = """
    SELECT i.id AS id, i.payload AS payload, rv.created_at AS ts
    FROM relevance_verdicts rv
    JOIN inbox i ON i.id = rv.inbox_id
    JOIN sources s ON s.id = i.source_id
    WHERE rv.user_id = :uid AND rv.verdict = 'not_relevant' AND rv.method = 'llm'
      AND s.type = ANY(:email_types)
"""
_ALLOW_QUALIFYING_SQL = """
    SELECT i.id AS id, i.payload AS payload, rv.created_at AS ts
    FROM relevance_verdicts rv
    JOIN inbox i ON i.id = rv.inbox_id
    JOIN sources s ON s.id = i.source_id
    WHERE rv.user_id = :uid AND rv.verdict = 'relevant' AND rv.method = 'llm'
      AND s.type = ANY(:email_types)
    UNION ALL
    SELECT i.id AS id, i.payload AS payload, rm.updated_at AS ts
    FROM relevance_marks rm
    JOIN inbox i ON i.id = rm.inbox_id
    JOIN sources s ON s.id = i.source_id
    WHERE rm.user_id = :uid AND rm.is_relevant = TRUE
      AND s.type = ANY(:email_types)
"""


@dataclass
class MiningStats:
    """Resultado de una corrida de minería. `senders` = clases (dominios) sobre el umbral."""

    senders: int = 0
    proposed: int = 0
    activated: int = 0
    rejected: int = 0
    skipped: int = 0
    cost: CostBySource = field(default_factory=CostBySource)


def _aggregate_by_sender(
    conn: Connection, user_id: int, effect: str, limit: int, min_messages: int
) -> list[dict[str, Any]]:
    """Agrega los correos fuente de la polaridad por DOMINIO del remitente (la «clase» que acumula):
    conteo + remitentes y asuntos de ejemplo + list_id. Solo clases con `min_messages`+ correos
    entran (un solo correo nunca propone nada; los que varían el local-part cuentan juntos). Toma
    los `limit` más recientes (dedup por inbox) para acotar el prompt.
    """
    qualifying = _ALLOW_QUALIFYING_SQL if effect == "allow" else _BLOCK_QUALIFYING_SQL
    rows = (
        conn.execute(
            text(
                f"""
                WITH qualifying AS (
                    {qualifying}
                ),
                recent AS (
                    SELECT DISTINCT ON (id) id, payload, ts FROM qualifying ORDER BY id, ts DESC
                ),
                limited AS (
                    SELECT payload FROM recent ORDER BY ts DESC LIMIT :limit
                ),
                per_mail AS (
                    SELECT lower(COALESCE(payload->'from'->>'email', '')) AS sender_email,
                           split_part(
                               lower(COALESCE(payload->'from'->>'email', '')), '@', 2
                           ) AS sender_domain,
                           COALESCE(payload->>'subject', '') AS subject,
                           lower(payload->>'list_id') AS list_id
                    FROM limited
                )
                SELECT sender_domain,
                       COUNT(*) AS messages,
                       (ARRAY_AGG(DISTINCT sender_email))[1:{_SENDER_SAMPLES}] AS sender_emails,
                       (ARRAY_AGG(DISTINCT subject))[1:{_SUBJECT_SAMPLES}] AS subject_samples,
                       (ARRAY_AGG(DISTINCT list_id)
                           FILTER (WHERE list_id IS NOT NULL))[1:3] AS list_ids
                FROM per_mail
                WHERE sender_domain <> ''
                GROUP BY sender_domain
                HAVING COUNT(*) >= :min_messages
                ORDER BY COUNT(*) DESC, sender_domain
                """
            ),
            {
                "uid": user_id,
                "email_types": EMAIL_TYPES,
                "limit": limit,
                "min_messages": min_messages,
            },
        )
        .mappings()
        .all()
    )
    return [
        {
            "sender_domain": r["sender_domain"],
            "messages": int(r["messages"]),
            "sender_emails": list(r["sender_emails"] or []),
            "subject_samples": list(r["subject_samples"] or []),
            "list_ids": list(r["list_ids"] or []),
        }
        for r in rows
    ]


async def run_rule_mining(
    user_id: int,
    *,
    effect: str = "block",
    limit: int = _DEFAULT_LIMIT,
    min_messages: int | None = None,
    client: LLMClient | None = None,
) -> MiningStats:
    """Propone reglas COMPUESTAS de una polaridad a partir del acumulado y las valida con dry run.

    Gate apagado → no-op (la minería es parte del gate). Umbral de acumulación: solo remitentes con
    `min_messages`+ correos (default: el setting del gate) entran al análisis; si ninguno llega,
    no-op SIN llamada LLM. Cada propuesta: inválida o duplicada → skip; dry run falla → `rejected`
    persistida con reporte; pasa → `active` (auto-activación auditada).
    """
    if effect not in EFFECTS:
        raise ValueError(f"effect inválido: {effect!r}; válidos: {EFFECTS}")
    stats = MiningStats()
    with connection() as conn:
        settings = get_settings(conn, user_id)
        if not settings.enabled:
            _log.info("relevance.mining.disabled", user_id=user_id, effect=effect)
            return stats
        threshold = min_messages if min_messages is not None else settings.mining_min_messages
        aggregates = _aggregate_by_sender(conn, user_id, effect, limit, threshold)
    if not aggregates:
        _log.info(
            "relevance.mining.below_threshold",
            user_id=user_id,
            effect=effect,
            min_messages=threshold,
        )
        return stats
    stats.senders = len(aggregates)

    messages = [
        ChatMessage("system", rules_system_prompt(effect)),
        ChatMessage("user", build_rules_user_content(json.dumps(aggregates, ensure_ascii=False))),
    ]
    owns_client = client is None
    active: LLMClient = client if client is not None else build_gate_client(settings)
    try:
        result = await active.complete(
            messages,
            model=settings.complete_model,
            response_format="json_object",
            max_tokens=_MAX_TOKENS,
        )
    finally:
        if owns_client and isinstance(active, AnthropicClient):
            await active.aclose()

    proposals = parse_rule_proposals(result.content) if result.content.strip() else None
    status = "ok" if proposals is not None else "error"
    record_llm_call(
        user_id=user_id,
        purpose="relevance_rules",
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cache_hit_tokens=result.usage.cache_hit_tokens,
        cost_usd=result.cost_usd,
        latency_ms=result.latency_ms,
        status=status,
        error_message=None if proposals is not None else "unparseable rule proposals",
        metadata={"effect": effect, "senders": stats.senders, "proposals": len(proposals or [])},
        response_text=result.content or None,
    )
    stats.cost.record(
        None,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
    )
    if proposals is None:
        _log.warning("relevance.mining.unparseable", user_id=user_id, effect=effect)
        return stats

    stats.proposed = len(proposals)
    with connection() as conn:
        for prop in proposals:
            try:
                report = dry_run_rule(
                    conn,
                    user_id,
                    effect=effect,
                    sender_kind=prop["sender_kind"],
                    sender_value=prop["sender_value"],
                    subject_pattern=prop["subject_pattern"],
                )
            except ValueError:  # propuesta mal formada que pasó el saneo de shape
                stats.skipped += 1
                continue
            row = create_rule(
                conn,
                user_id,
                effect=effect,
                sender_kind=prop["sender_kind"],
                sender_value=prop["sender_value"],
                subject_pattern=prop["subject_pattern"],
                proposed_by="llm",
                report=report,
                rationale=prop["rationale"],
                model=result.model,
            )
            if row is None:
                stats.skipped += 1
                continue
            if row["status"] == "active":
                stats.activated += 1
            else:
                stats.rejected += 1
            _log.info(
                "relevance.mining.rule",
                effect=effect,
                rule_id=row["id"],
                sender_kind=prop["sender_kind"],
                sender_value=prop["sender_value"],
                subject_pattern=prop["subject_pattern"],
                status=row["status"],
                matched=report.matched,
                matched_relevant=report.matched_relevant,
                matched_not_relevant=report.matched_not_relevant,
            )

    _log.info(
        "relevance.mining.end",
        user_id=user_id,
        effect=effect,
        senders=stats.senders,
        proposed=stats.proposed,
        activated=stats.activated,
        rejected=stats.rejected,
        skipped=stats.skipped,
        **stats.cost.log_fields(),
    )
    return stats


async def run_rule_mining_cycle(
    user_id: int,
    *,
    limit: int = _DEFAULT_LIMIT,
    min_messages: int | None = None,
    client: LLMClient | None = None,
) -> MiningStats:
    """Mina AMBAS polaridades (block + allow) en una corrida y combina las stats.

    Es el callable del job del scheduler (`relevance_rules`) y del intercalado entre ventanas:
    el mismo lazo que aprende qué bloquear aprende qué dejar pasar.
    """
    combined = MiningStats()
    for effect in EFFECTS:
        s = await run_rule_mining(
            user_id, effect=effect, limit=limit, min_messages=min_messages, client=client
        )
        combined.senders += s.senders
        combined.proposed += s.proposed
        combined.activated += s.activated
        combined.rejected += s.rejected
        combined.skipped += s.skipped
        total = s.cost.total
        if total.calls:
            combined.cost.record(
                None,
                prompt_tokens=total.prompt_tokens,
                completion_tokens=total.completion_tokens,
                cost_usd=total.cost_usd,
            )
    return combined
