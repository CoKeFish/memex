"""Reglas de CHAT para el directorio: quién gana ficha según la NATURALEZA del chat.

La naturaleza viene de la allowlist de la fuente (`chat_context`): `intimo` → todo cuenta y todos
ganan ficha; `programa` y `anuncios` → SOLO remitentes (recurrentes / el propio usuario, ver
`senders.py`). Lo NOMBRADO en el texto de un chat no íntimo NUNCA gana ficha: si ya existe en el
directorio se ata (difuso alto); si no, queda como avistamiento sin identidad.

Antes esa regla dependía de que el LLM marcara la mención como `mencion`: en un grupo de anuncios
marcó a los organizadores de eventos ajenos como contrapartes y nacieron 235 organizaciones que no
son del dueño (primera corrida real de WhatsApp, 2026-09-13). Ahora la decide el código.

`prune_chat_mention_identities` re-aplica la regla sobre lo ya cargado (como `remark` en finanzas):
una ficha nacida por extracción cuyas menciones vienen TODAS de chats no íntimos, sin mención de
remitente, sin identificadores ni dependientes, vuelve a ser avistamiento (`drop_identity`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.modules.chat_context import ChatModes, chat_modes_for_source
from memex.relations.edges import CANAL_SLUG, Ref
from memex.relations.graph_writer import delete_vertex

_log = get_logger("memex.modules.identidades.chat_rules")

#: Tipos de fuente que son CHAT (con allowlist y naturaleza por chat).
CHAT_TYPES: tuple[str, ...] = ("telegram", "whatsapp")


def chat_mode_by_inbox(
    conn: Connection, user_id: int, inbox_ids: Sequence[int]
) -> dict[int, str | None]:
    """`inbox.id → naturaleza del chat` para los mensajes de chat del lote; `None` si el mensaje
    no es de un chat (correo, social, documento)."""
    ids = list(inbox_ids)
    if not ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT i.id, i.source_id, s.type, i.payload->>'chat_id' AS chat_id "
            "FROM inbox i JOIN sources s ON s.id = i.source_id "
            "WHERE i.user_id = :u AND i.id = ANY(:ids)"
        ),
        {"u": user_id, "ids": ids},
    ).all()
    modes: dict[int, ChatModes] = {}
    out: dict[int, str | None] = {}
    for inbox_id, source_id, stype, chat_id in rows:
        if str(stype) not in CHAT_TYPES:
            out[int(inbox_id)] = None
            continue
        sid = int(source_id)
        if sid not in modes:
            modes[sid] = chat_modes_for_source(conn, sid)
        out[int(inbox_id)] = modes[sid].of_chat(chat_id)
    return out


def strictest_mode(modes: Iterable[str | None]) -> str | None:
    """De varios mensajes citados, la naturaleza que manda: la más restrictiva."""
    ms = {m for m in modes if m is not None}
    if "anuncios" in ms:
        return "anuncios"
    if "programa" in ms:
        return "programa"
    if "intimo" in ms:
        return "intimo"
    return None


def mention_may_create(mode: str | None) -> bool:
    """¿Una mención en un mensaje de esta naturaleza puede CREAR ficha? Solo fuera de chats (correo,
    social…) o en un chat íntimo."""
    return mode is None or mode == "intimo"


@dataclass
class PruneStats:
    examined: int = 0
    dropped: int = 0
    refused: int = 0
    canales_fixed: int = 0
    dry_run: bool = False
    dropped_names: list[str] = field(default_factory=list)


def prune_chat_mention_identities(
    conn: Connection, user_id: int, *, dry_run: bool = False
) -> PruneStats:
    """Re-aplica la regla sobre el directorio: fichas de extracción sostenidas SOLO por menciones de
    chats no íntimos (ninguna mención de remitente, ningún identificador) → avistamiento. Además
    corrige canales de WhatsApp registrados como `telegram` (bug previo: `sync_canales` fijaba la
    plataforma), borrando el vértice por `graph_writer`."""
    from memex.modules.identidades.verify_llm import drop_identity

    stats = PruneStats(dry_run=dry_run)
    candidates = conn.execute(
        text(
            """
            SELECT x.id, x.display_name
            FROM mod_identidades x
            WHERE x.user_id = :u AND x.source = 'extraction'
              AND NOT EXISTS (
                    SELECT 1 FROM mod_identidades_identifiers f WHERE f.identity_id = x.id)
              AND EXISTS (
                    SELECT 1 FROM mod_identidades_mentions m WHERE m.resolved_identity_id = x.id)
              AND NOT EXISTS (
                    SELECT 1 FROM mod_identidades_mentions m
                    WHERE m.resolved_identity_id = x.id AND m.resolution_method = 'sender')
            ORDER BY x.id
            """
        ),
        {"u": user_id},
    ).all()
    for iid, name in candidates:
        stats.examined += 1
        inbox_ids = [
            int(r[0])
            for r in conn.execute(
                text(
                    "SELECT DISTINCT unnest(source_inbox_ids) FROM mod_identidades_mentions "
                    "WHERE resolved_identity_id = :i"
                ),
                {"i": int(iid)},
            ).all()
        ]
        modes = chat_mode_by_inbox(conn, user_id, inbox_ids)
        if not inbox_ids or len(modes) < len(inbox_ids):
            continue  # alguna mención sin mensaje: no es un caso limpio, se deja
        if any(mention_may_create(modes[i]) for i in inbox_ids):
            continue  # al menos una mención en correo/social o chat íntimo: la ficha se sostiene
        if dry_run:
            stats.dropped += 1
            stats.dropped_names.append(str(name))
            continue
        if drop_identity(conn, user_id, int(iid)):
            stats.dropped += 1
            stats.dropped_names.append(str(name))
        else:
            stats.refused += 1

    wrong_canales = conn.execute(
        text(
            "SELECT id FROM mod_canales WHERE user_id = :u AND platform = 'telegram' "
            "AND external_id LIKE '%@%'"
        ),
        {"u": user_id},
    ).all()
    for (cid,) in wrong_canales:
        stats.canales_fixed += 1
        if dry_run:
            continue
        delete_vertex(conn, user_id, Ref(CANAL_SLUG, int(cid)))
        conn.execute(text("DELETE FROM mod_canales WHERE id = :c"), {"c": int(cid)})

    _log.info(
        "identidades.chat_rules.prune",
        user_id=user_id,
        examined=stats.examined,
        dropped=stats.dropped,
        refused=stats.refused,
        canales_fixed=stats.canales_fixed,
        dry_run=dry_run,
    )
    return stats
