"""Sync de CANALES de chat: upsert determinista de `mod_canales` desde los payloads de inbox.

El canal es la entidad-contexto de un chat (grupo curado por allowlist). La tabla es del grafo
(como `relation_clusters`): no la escribe ningún módulo de extracción — se DERIVA del payload
(`chat_id`/`chat_title`/`chat_kind`), idempotente, con el título más reciente (los grupos se
renombran). La teje `weave_chat_structure` por LOTE al procesar un chat (paso 5), acotada por
`inbox_ids`; sin scope barre todo el user.

Solo Telegram por ahora (el único chat ingestado); la identidad natural `(platform, external_id)`
deja la puerta abierta a otras plataformas sin migración.

RIESGO conocido (decisión del usuario: el canal SÍ clusteriza — sus chats son contextos
concretos): un canal activo es hub (pistas hacia todo lo extraído del chat + `participa_en` 1.0
hacia cada remitente) → puede armar blobs > `cluster_max_members` que el partidor salta
(`relation.cluster.oversize` + skip). Mitigación en orden: la recursión Louvain existente →
`MEMEX_CLUSTER_EXCLUDE_CANAL=true` → bajar `cluster_w_pista`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger

_log = get_logger("memex.relations.canales")


def sync_canales(conn: Connection, user_id: int, inbox_ids: Sequence[int] | None = None) -> int:
    """Upsert de los canales de CHAT del user desde inbox (idempotente; actualiza
    `display_name`/`chat_kind` al valor más reciente). Con `inbox_ids` acota a los canales de esos
    mensajes (tejido por-lote); sin él, barre todo el user. Devuelve cuántos canales viven."""
    scope = "" if inbox_ids is None else " AND i.id = ANY(:ids)"
    params: dict[str, Any] = {"u": user_id}
    if inbox_ids is not None:
        params["ids"] = list(inbox_ids)
    conn.execute(
        text(
            f"""
            INSERT INTO mod_canales (user_id, platform, external_id, display_name, chat_kind)
            SELECT DISTINCT ON (i.payload->>'chat_id')
                   :u, 'telegram', i.payload->>'chat_id',
                   COALESCE(i.payload->>'chat_title', ''), i.payload->>'chat_kind'
            FROM inbox i
            WHERE i.user_id = :u AND i.payload->>'chat_id' IS NOT NULL{scope}
            ORDER BY i.payload->>'chat_id', i.id DESC
            ON CONFLICT (user_id, platform, external_id)
            DO UPDATE SET display_name = EXCLUDED.display_name,
                          chat_kind   = EXCLUDED.chat_kind,
                          updated_at  = NOW()
            """
        ),
        params,
    )
    n = int(
        conn.execute(
            text("SELECT count(*) FROM mod_canales WHERE user_id = :u"), {"u": user_id}
        ).scalar_one()
    )
    if n:
        _log.info("relation.canales.sync", user_id=user_id, canales=n)
    return n
