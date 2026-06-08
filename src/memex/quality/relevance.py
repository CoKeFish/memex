"""Relevancia por remitente — lectura agregada determinista (sistema de calidad).

Sin LLM, SQL puro (patrón `core/feedback.py` / `api/routers/metrics.py`). Por cada remitente agrega:
cuántos mensajes, cuántos produjeron un hecho de dominio (señal núcleo: existe fila en
`module_extractions` de un módulo != `identidades` con `item_count>0`), cuántos SOLO se resumieron
(valor de lectura — bucket aparte para no lavar la señal) y cuántos quedaron inertes (ni hecho ni
resumen). El `%` de relevancia cuenta solo los que produjeron hecho; `volume_ratio` = mensajes del
remitente / media por remitente. Sin umbrales: ordena ruido (inerte) primero y el front re-ordena.

`sender_key` agrupa de forma estable (email normalizado; telegram por `sender.user_id` o, sin
sender, por chat; social por cuenta) con prefijo de namespace para no colisionar entre fuentes;
`sender_label` es la etiqueta legible.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

#: Agregación por remitente. `{source_filter}` se inyecta como cláusula AND opcional (acota a una
#: fuente); el resto va por params ligados (sin interpolar input del usuario).
_SENDERS_SQL = """
WITH msg AS (
    SELECT
        i.id AS inbox_id,
        i.occurred_at AS occurred_at,
        COALESCE(
            lower(i.payload->'from'->>'email'),
            'tg:user:' || (i.payload->'sender'->>'user_id'),
            'social:' || NULLIF(i.payload->>'account', ''),
            'tg:chat:' || NULLIF(i.payload->>'chat_id', ''),
            '(desconocido)'
        ) AS sender_key,
        COALESCE(
            i.payload->'from'->>'email',
            NULLIF(i.payload->'sender'->>'display_name', ''),
            NULLIF(i.payload->'sender'->>'username', ''),
            NULLIF(i.payload->>'account_name', ''),
            NULLIF(i.payload->>'account', ''),
            NULLIF(i.payload->>'chat_title', ''),
            NULLIF(i.payload->>'chat_id', ''),
            '(desconocido)'
        ) AS sender_label,
        c.tier AS tier,
        EXISTS (
            SELECT 1 FROM module_extractions me
            WHERE me.inbox_id = i.id AND me.module_slug <> 'identidades' AND me.item_count > 0
        ) AS produced_fact,
        EXISTS (
            SELECT 1 FROM summary_inbox_links sl WHERE sl.inbox_id = i.id
        ) AS summarized
    FROM inbox i
    LEFT JOIN classifications c ON c.inbox_id = i.id
    WHERE i.user_id = :uid
      {source_filter}
),
agg AS (
    SELECT
        sender_key,
        max(sender_label) AS sender_label,
        count(*) AS messages,
        count(*) FILTER (WHERE produced_fact) AS relevant,
        count(*) FILTER (WHERE NOT produced_fact AND summarized) AS summarized_only,
        count(*) FILTER (WHERE NOT produced_fact AND NOT summarized) AS inert,
        max(occurred_at) AS last_at,
        count(*) FILTER (WHERE tier = 'blacklist') AS tier_blacklist,
        count(*) FILTER (WHERE tier = 'batch') AS tier_batch,
        count(*) FILTER (WHERE tier = 'individual') AS tier_individual,
        count(*) FILTER (WHERE tier IS NULL) AS tier_unclassified
    FROM msg
    GROUP BY sender_key
)
SELECT
    sender_key,
    sender_label,
    messages,
    relevant,
    summarized_only,
    inert,
    round(100.0 * relevant / NULLIF(messages, 0), 1) AS relevance_pct,
    last_at,
    jsonb_build_object(
        'blacklist', tier_blacklist, 'batch', tier_batch,
        'individual', tier_individual, 'unclassified', tier_unclassified
    ) AS tier_mix,
    round(messages::numeric / NULLIF(avg(messages) OVER (), 0), 2) AS volume_ratio
FROM agg
ORDER BY inert DESC, messages DESC
LIMIT :limit
"""


def senders_by_relevance(
    conn: Connection,
    *,
    user_id: int,
    limit: int = 200,
    source_id: int | None = None,
) -> list[dict[str, Any]]:
    """Remitentes rankeados por relevancia (ruido primero). `source_id` acota a una fuente."""
    params: dict[str, Any] = {"uid": user_id, "limit": limit}
    source_filter = ""
    if source_id is not None:
        source_filter = "AND i.source_id = :sid"
        params["sid"] = source_id
    rows = conn.execute(text(_SENDERS_SQL.format(source_filter=source_filter)), params).mappings()
    return [dict(r) for r in rows.all()]
