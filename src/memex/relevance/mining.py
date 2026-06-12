"""Minería de reglas: segunda pasada del gate sobre los correos NO relevantes.

El comportamiento definido por el dueño: el sistema revisa una segunda vez los correos que no
fueron relevantes, busca patrones y propone reglas deterministas; cada propuesta se valida con
un DRY RUN contra el histórico — si atraparía algún correo relevante, la regla está mal hecha
(queda `rejected` con su reporte). Si pasa, se AUTO-ACTIVA (auditada y reversible).

Una sola llamada LLM por corrida (`purpose="relevance_rules"`): recibe el AGREGADO de los
no-relevantes por DOMINIO del remitente (conteos + remitentes y asuntos de ejemplo +
list_id), no los correos crudos. La garantía de seguridad NO es el LLM: es el dry run
determinista de `rules.dry_run_rule`.
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
    RULES_SYSTEM_PROMPT,
    build_rules_user_content,
    parse_rule_proposals,
)
from memex.relevance.providers import build_gate_client
from memex.relevance.rules import create_rule, dry_run_rule
from memex.relevance.settings import get_settings
from memex.relevance.verdicts import EMAIL_TYPES

_log = get_logger("memex.relevance.mining")

#: Tope de mensajes no-relevantes que alimentan el agregado (los más recientes).
_DEFAULT_LIMIT = 500
#: Asuntos de ejemplo por remitente en el agregado (suficiente para ver la plantilla).
_SUBJECT_SAMPLES = 5
_MAX_TOKENS = 2048


@dataclass
class MiningStats:
    """Resultado de una corrida de minería. `senders` = clases (dominios) sobre el umbral."""

    senders: int = 0
    proposed: int = 0
    activated: int = 0
    rejected: int = 0
    skipped: int = 0
    cost: CostBySource = field(default_factory=CostBySource)


def _aggregate_not_relevant(
    conn: Connection, user_id: int, limit: int, min_messages: int
) -> list[dict[str, Any]]:
    """Agrega los no-relevantes del gate por DOMINIO del remitente (la «clase» que acumula):
    conteo + remitentes y asuntos de ejemplo + list_id.

    Umbral de acumulación a nivel dominio: solo clases con `min_messages`+ no-relevantes
    entran al agregado — un solo correo malo nunca propone nada, y remitentes que varían el
    local-part (a@spam.io, b@spam.io) cuentan juntos. Solo veredictos del LLM
    (`method='llm'`): los `manual` son juicio del dueño sobre casos puntuales y los `rule` ya
    están cubiertos por una regla — esa clase está resuelta, no se vuelve a analizar. Toma los
    `limit` más recientes para acotar el prompt.
    """
    rows = (
        conn.execute(
            text(
                f"""
                WITH recent AS (
                    SELECT i.payload
                    FROM relevance_verdicts rv
                    JOIN inbox i ON i.id = rv.inbox_id
                    JOIN sources s ON s.id = i.source_id
                    WHERE rv.user_id = :uid
                      AND rv.verdict = 'not_relevant'
                      AND rv.method = 'llm'
                      AND s.type = ANY(:email_types)
                    ORDER BY rv.created_at DESC
                    LIMIT :limit
                ),
                per_mail AS (
                    SELECT lower(COALESCE(payload->'from'->>'email', '')) AS sender_email,
                           split_part(
                               lower(COALESCE(payload->'from'->>'email', '')), '@', 2
                           ) AS sender_domain,
                           COALESCE(payload->>'subject', '') AS subject,
                           lower(payload->>'list_id') AS list_id
                    FROM recent
                )
                SELECT sender_domain,
                       COUNT(*) AS messages,
                       (ARRAY_AGG(DISTINCT sender_email))[1:{_SUBJECT_SAMPLES}] AS sender_emails,
                       (ARRAY_AGG(DISTINCT subject))[1:{_SUBJECT_SAMPLES}] AS subject_samples,
                       (ARRAY_AGG(DISTINCT list_id)
                           FILTER (WHERE list_id IS NOT NULL))[1:3] AS list_ids
                FROM per_mail
                GROUP BY 1
                HAVING COUNT(*) >= :min_messages
                ORDER BY COUNT(*) DESC, 1
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
    limit: int = _DEFAULT_LIMIT,
    min_messages: int | None = None,
    client: LLMClient | None = None,
) -> MiningStats:
    """Propone reglas a partir del ACUMULADO de no-relevantes y las valida con dry run.

    Gate apagado → no-op (la minería es parte del gate). Umbral de acumulación: solo
    remitentes con `min_messages`+ no-relevantes (default: el setting del gate) entran al
    análisis; si ninguno llega, no-op SIN llamada LLM — un solo correo malo nunca dispara
    nada. Cada propuesta: duplicada → skip; dry run falla → `rejected` persistida con
    reporte; pasa → `active` (auto-activación auditada).
    """
    stats = MiningStats()
    with connection() as conn:
        settings = get_settings(conn, user_id)
        if not settings.enabled:
            _log.info("relevance.mining.disabled", user_id=user_id)
            return stats
        threshold = min_messages if min_messages is not None else settings.mining_min_messages
        aggregates = _aggregate_not_relevant(conn, user_id, limit, threshold)
    if not aggregates:
        _log.info("relevance.mining.below_threshold", user_id=user_id, min_messages=threshold)
        return stats
    stats.senders = len(aggregates)

    messages = [
        ChatMessage("system", RULES_SYSTEM_PROMPT),
        ChatMessage("user", build_rules_user_content(json.dumps(aggregates, ensure_ascii=False))),
    ]
    owns_client = client is None
    active: LLMClient = client if client is not None else build_gate_client(settings)
    try:
        result = await active.complete(
            messages,
            model=settings.model,
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
        metadata={"senders": stats.senders, "proposals": len(proposals or [])},
        response_text=result.content or None,
    )
    stats.cost.record(
        None,
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        cost_usd=result.cost_usd,
    )
    if proposals is None:
        _log.warning("relevance.mining.unparseable", user_id=user_id)
        return stats

    stats.proposed = len(proposals)
    with connection() as conn:
        for prop in proposals:
            report = dry_run_rule(conn, user_id, prop["kind"], prop["pattern"])
            row = create_rule(
                conn,
                user_id,
                kind=prop["kind"],
                pattern=prop["pattern"],
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
                rule_id=row["id"],
                kind=prop["kind"],
                pattern=prop["pattern"],
                status=row["status"],
                matched=report.matched,
                matched_relevant=report.matched_relevant,
            )

    _log.info(
        "relevance.mining.end",
        user_id=user_id,
        senders=stats.senders,
        proposed=stats.proposed,
        activated=stats.activated,
        rejected=stats.rejected,
        skipped=stats.skipped,
        **stats.cost.log_fields(),
    )
    return stats
