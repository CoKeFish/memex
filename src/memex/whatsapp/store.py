"""Todo el SQL de WhatsApp: vínculo, catálogo de chats, bandeja, allowlist y fuente.

Funciones síncronas sobre una `Connection` (el runtime las corre en un hilo; el router en el
request). La fuente es una por cuenta (`sources.type='whatsapp'`, `account_id`), y la selección de
chats vive en `sources.config.allowed_chats` con el formato de Telegram (ver `config.py`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.api.ingest_service import decode_media, ingest_one_record, persist_media
from memex.api.schemas import IngestRequest, MediaItem
from memex.core.inbox import invalidate_for_reprocess
from memex.core.payloads import WhatsAppPayload, WhatsAppSender
from memex.core.source import MediaBlob
from memex.ingestors.whatsapp.config import (
    WaAllowedChat,
    allowed_chats_to_config,
    parse_allowed_chats,
)
from memex.ingestors.whatsapp.parser import WaMessage, external_id
from memex.logging import get_logger

_log = get_logger("memex.whatsapp.store")

LINK_FIELDS = (
    "phone",
    "jid",
    "status",
    "pair_code",
    "pair_code_at",
    "linked_at",
    "last_event_at",
    "last_error",
)


# ------------------------------------------------------------------------------------- fuente
def ensure_source(conn: Connection, user_id: int, account_id: int) -> dict[str, Any]:
    """Get-or-create de la fuente `whatsapp` de la cuenta (idempotente; devuelve id + config)."""
    existing = (
        conn.execute(
            text(
                "SELECT id, config FROM sources "
                "WHERE user_id = :uid AND type = 'whatsapp' AND account_id = :aid"
            ),
            {"uid": user_id, "aid": account_id},
        )
        .mappings()
        .first()
    )
    if existing is not None:
        return dict(existing)
    alias = conn.execute(
        text("SELECT alias FROM accounts WHERE id = :aid"), {"aid": account_id}
    ).scalar()
    name = f"WhatsApp · {alias}" if alias else f"WhatsApp (cuenta {account_id})"
    row = (
        conn.execute(
            text(
                "INSERT INTO sources (user_id, name, type, config, account_id) "
                "VALUES (:uid, :name, 'whatsapp', '{}'::jsonb, :aid) RETURNING id, config"
            ),
            {"uid": user_id, "name": name, "aid": account_id},
        )
        .mappings()
        .one()
    )
    _log.info("whatsapp.source.created", account_id=account_id, source_id=row["id"])
    return dict(row)


def load_allowed(conn: Connection, source_id: int) -> dict[str, WaAllowedChat]:
    cfg = conn.execute(
        text("SELECT config FROM sources WHERE id = :sid"), {"sid": source_id}
    ).scalar()
    return parse_allowed_chats(cfg if isinstance(cfg, dict) else None)


def save_allowed(conn: Connection, source_id: int, chats: list[WaAllowedChat]) -> None:
    """Reemplaza `allowed_chats` en la config de la fuente sin tocar el resto."""
    conn.execute(
        text(
            "UPDATE sources SET config = COALESCE(config, '{}'::jsonb) || "
            "jsonb_build_object('allowed_chats', CAST(:chats AS JSONB)) WHERE id = :sid"
        ),
        {"sid": source_id, "chats": json.dumps(allowed_chats_to_config(chats))},
    )


# ------------------------------------------------------------------------------------- vínculo
def get_link(conn: Connection, account_id: int) -> dict[str, Any] | None:
    row = (
        conn.execute(
            text("SELECT * FROM whatsapp_links WHERE account_id = :aid"), {"aid": account_id}
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def upsert_link(conn: Connection, user_id: int, account_id: int, **fields: Any) -> None:
    bad = set(fields) - set(LINK_FIELDS)
    if bad:
        raise ValueError(f"campos desconocidos de whatsapp_links: {sorted(bad)}")
    cols = ", ".join(fields)
    vals = ", ".join(f":{k}" for k in fields)
    sets = ", ".join(f"{k} = EXCLUDED.{k}" for k in fields)
    conn.execute(
        text(
            f"INSERT INTO whatsapp_links (account_id, user_id{', ' + cols if cols else ''}) "
            f"VALUES (:aid, :uid{', ' + vals if vals else ''}) "
            f"ON CONFLICT (account_id) DO UPDATE SET {sets + ', ' if sets else ''}"
            "last_event_at = NOW(), updated_at = NOW()"
        ),
        {"aid": account_id, "uid": user_id, **fields},
    )


def set_link_status(conn: Connection, account_id: int, status: str, **extra: Any) -> None:
    sets = ["status = :status", "last_event_at = NOW()", "updated_at = NOW()"]
    params: dict[str, Any] = {"aid": account_id, "status": status}
    for k, v in extra.items():
        if k not in LINK_FIELDS:
            raise ValueError(f"campo desconocido de whatsapp_links: {k}")
        sets.append(f"{k} = :{k}")
        params[k] = v
    conn.execute(
        text(f"UPDATE whatsapp_links SET {', '.join(sets)} WHERE account_id = :aid"), params
    )


def set_link_error(conn: Connection, account_id: int, error: str) -> None:
    conn.execute(
        text(
            "UPDATE whatsapp_links SET last_error = :e, last_event_at = NOW(), updated_at = NOW() "
            "WHERE account_id = :aid"
        ),
        {"aid": account_id, "e": error[:500]},
    )


def linked_accounts(conn: Connection) -> list[tuple[int, int]]:
    """Cuentas que el lifespan debe conectar: vinculadas o caídas (reconectan solas)."""
    rows = conn.execute(
        text(
            "SELECT l.account_id, l.user_id FROM whatsapp_links l "
            "JOIN accounts a ON a.id = l.account_id "
            "WHERE l.status IN ('vinculado', 'desconectado') AND a.enabled ORDER BY l.account_id"
        )
    ).all()
    return [(int(a), int(u)) for a, u in rows]


# ------------------------------------------------------------------------------------- catálogo
def upsert_chat(
    conn: Connection,
    user_id: int,
    account_id: int,
    jid: str,
    *,
    name: str,
    kind: str,
    new_messages: int,
    first_at: datetime | None,
    last_at: datetime | None,
    description: str | None = None,
) -> None:
    """`description=None` no toca la descripción guardada; '' la borra (el grupo la quitó)."""
    conn.execute(
        text(
            """
            INSERT INTO whatsapp_chats
              (user_id, account_id, jid, name, kind, msg_count, first_at, last_at, description)
            VALUES (:uid, :aid, :jid, :name, :kind, :n, :first_at, :last_at, COALESCE(:desc, ''))
            ON CONFLICT (account_id, jid) DO UPDATE SET
              name = CASE WHEN EXCLUDED.name <> '' THEN EXCLUDED.name ELSE whatsapp_chats.name END,
              description = COALESCE(CAST(:desc AS TEXT), whatsapp_chats.description),
              kind = EXCLUDED.kind,
              msg_count = whatsapp_chats.msg_count + EXCLUDED.msg_count,
              first_at = LEAST(whatsapp_chats.first_at, EXCLUDED.first_at),
              last_at = GREATEST(whatsapp_chats.last_at, EXCLUDED.last_at),
              updated_at = NOW()
            """
        ),
        {
            "uid": user_id,
            "aid": account_id,
            "jid": jid,
            "name": name or "",
            "kind": kind,
            "n": int(new_messages),
            "first_at": first_at,
            "last_at": last_at,
            "desc": description,
        },
    )


def chat_names(conn: Connection, account_id: int) -> dict[str, str]:
    rows = conn.execute(
        text("SELECT jid, name FROM whatsapp_chats WHERE account_id = :aid AND name <> ''"),
        {"aid": account_id},
    ).all()
    return {str(j): str(n) for j, n in rows}


def chat_description(conn: Connection, account_id: int, jid: str) -> str:
    desc = conn.execute(
        text("SELECT description FROM whatsapp_chats WHERE account_id = :aid AND jid = :jid"),
        {"aid": account_id, "jid": jid},
    ).scalar()
    return str(desc or "")


def description_external_id(jid: str, description: str) -> str:
    digest = hashlib.sha256(" ".join(description.split()).encode("utf-8")).hexdigest()[:8]
    return f"whatsapp:{jid}:descripcion:{digest}"


def ingest_description(
    conn: Connection,
    user_id: int,
    source_id: int,
    *,
    jid: str,
    name: str,
    description: str,
    when: datetime,
) -> Ingested:
    """La descripción del grupo entra al inbox como un mensaje del propio grupo (sin remitente):
    trae enlaces a carpetas, reglas, fechas. Una por VERSIÓN (el id lleva el hash del texto): si la
    descripción cambia entra la nueva; repetirla no duplica."""
    clean = description.strip()
    if not clean:
        return Ingested(inserted=False, inbox_id=None)
    eid = description_external_id(jid, clean)
    payload = WhatsAppPayload(
        chat_id=jid,
        chat_kind="group" if jid.endswith("@g.us") else "user",
        chat_title=name or None,
        message_id=eid.rsplit(":", 2)[-2] + ":" + eid.rsplit(":", 1)[-1],
        sender=WhatsAppSender(phone=None, display_name="descripción del grupo", is_self=False),
        date=when,
        text=f"Descripción del grupo «{name}»:\n{clean}" if name else clean,
    )
    req = IngestRequest(
        source_id=source_id,
        external_id=eid,
        occurred_at=when,
        payload=payload.model_dump(mode="json", by_alias=True),
        dedupe_keys=[eid],
        media=[],
    )
    outcome = ingest_one_record(conn, user_id, req)
    inbox_id = outcome.id
    if inbox_id is None and outcome.reason == "duplicate":
        inbox_id = conn.execute(
            text("SELECT id FROM inbox WHERE source_id = :s AND external_id = :e"),
            {"s": source_id, "e": eid},
        ).scalar()
    if outcome.inserted:
        _log.info("whatsapp.description.ingested", user_id=user_id, jid=jid, inbox_id=inbox_id)
    return Ingested(
        inserted=bool(outcome.inserted),
        inbox_id=int(inbox_id) if inbox_id is not None else None,
    )


def chat_name(conn: Connection, account_id: int, jid: str) -> str:
    name = conn.execute(
        text("SELECT name FROM whatsapp_chats WHERE account_id = :aid AND jid = :jid"),
        {"aid": account_id, "jid": jid},
    ).scalar()
    return str(name or "")


def list_chats(
    conn: Connection, account_id: int, allowed: dict[str, WaAllowedChat]
) -> list[dict[str, Any]]:
    """Catálogo + estado de inclusión, ordenado por actividad reciente.

    `msg_count` es lo que HAY de ese chat (bandeja + inbox), no el acumulador `whatsapp_chats.
    msg_count`: ese suma cada vez que WhatsApp reenvía el historial (p. ej. al re-vincular) y
    quedó inflado en la primera vinculación real; la bandeja y el inbox deduplican por id."""
    rows = (
        conn.execute(
            text(
                "SELECT c.jid, c.name, c.kind, c.first_at, c.last_at, "
                "(SELECT count(*) FROM whatsapp_buffer b WHERE b.account_id = c.account_id "
                " AND b.chat_jid = c.jid) AS en_bandeja "
                "FROM whatsapp_chats c WHERE c.account_id = :aid "
                "ORDER BY c.last_at DESC NULLS LAST, c.name"
            ),
            {"aid": account_id},
        )
        .mappings()
        .all()
    )
    ingested: dict[str, int] = {
        str(jid): int(n)
        for jid, n in conn.execute(
            text(
                "SELECT i.payload->>'chat_id', count(*) FROM inbox i "
                "JOIN sources s ON s.id = i.source_id "
                "WHERE s.type = 'whatsapp' AND s.account_id = :aid GROUP BY 1"
            ),
            {"aid": account_id},
        ).all()
    }
    out: list[dict[str, Any]] = []
    for r in rows:
        jid = str(r["jid"])
        sel = allowed.get(jid)
        out.append(
            {
                "chat_id": jid,
                "name": str(r["name"] or ""),
                "kind": str(r["kind"]),
                "msg_count": int(r["en_bandeja"]) + ingested.get(jid, 0),
                "first_at": r["first_at"],
                "last_at": r["last_at"],
                "buffered": int(r["en_bandeja"]),
                "included": sel is not None,
                "mode": sel.mode if sel else None,
                "since": sel.since.isoformat() if sel and sel.since else None,
            }
        )
    return out


# ------------------------------------------------------------------------------------- bandeja
def buffer_put(conn: Connection, user_id: int, account_id: int, wm: WaMessage) -> bool:
    raw = wm.message.SerializeToString() if wm.message is not None else b""
    if not raw or not wm.message_id:
        return False
    inserted = conn.execute(
        text(
            """
            INSERT INTO whatsapp_buffer
              (user_id, account_id, chat_jid, message_id, from_me, sender, push_name,
               occurred_at, raw)
            VALUES (:uid, :aid, :jid, :mid, :from_me, :sender, :push, :at, :raw)
            ON CONFLICT (account_id, chat_jid, message_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "uid": user_id,
            "aid": account_id,
            "jid": wm.chat_jid,
            "mid": wm.message_id,
            "from_me": wm.from_me,
            "sender": wm.sender,
            "push": wm.push_name,
            "at": wm.occurred_at,
            "raw": raw,
        },
    ).scalar()
    return inserted is not None


def buffer_drain(conn: Connection, account_id: int, chat_jid: str) -> list[dict[str, Any]]:
    """Saca (y borra) la bandeja de un chat, en orden cronológico."""
    rows = (
        conn.execute(
            text(
                "DELETE FROM whatsapp_buffer WHERE account_id = :aid AND chat_jid = :jid "
                "RETURNING message_id, from_me, sender, push_name, occurred_at, raw"
            ),
            {"aid": account_id, "jid": chat_jid},
        )
        .mappings()
        .all()
    )
    return sorted((dict(r) for r in rows), key=lambda r: r["occurred_at"])


def buffer_purge(conn: Connection, account_id: int, retention_days: int) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    res = conn.execute(
        text("DELETE FROM whatsapp_buffer WHERE account_id = :aid AND received_at < :cutoff"),
        {"aid": account_id, "cutoff": cutoff},
    )
    return int(res.rowcount or 0)


def buffer_count(conn: Connection, account_id: int) -> int:
    return int(
        conn.execute(
            text("SELECT count(*) FROM whatsapp_buffer WHERE account_id = :aid"),
            {"aid": account_id},
        ).scalar_one()
    )


# ------------------------------------------------------------------------------------- inbox
@dataclass(frozen=True)
class Ingested:
    """Resultado de `ingest`: si la fila es nueva, y su id (la nueva o la que ya existía)."""

    inserted: bool
    inbox_id: int | None


def ingest(
    conn: Connection,
    user_id: int,
    source_id: int,
    wm: WaMessage,
    payload: WhatsAppPayload,
    media: list[MediaItem],
) -> Ingested:
    """Un mensaje al inbox por la puerta de siempre (dedup por external_id; medios a MinIO). Si ya
    estaba, devuelve igual su id: el que llama decide si le falta algo (el archivo)."""
    eid = external_id(wm)
    req = IngestRequest(
        source_id=source_id,
        external_id=eid,
        occurred_at=wm.occurred_at,
        payload=payload.model_dump(mode="json", by_alias=True),
        dedupe_keys=[eid],
        media=media,
    )
    outcome = ingest_one_record(conn, user_id, req)
    inbox_id = outcome.id
    if inbox_id is None and outcome.reason == "duplicate":
        # La clave `whatsapp:<chat>:<id>` es identidad del mensaje para el usuario: si ya entró
        # por otra fuente (cuenta re-creada, segundo número en el mismo grupo), ESA es la fila.
        inbox_id = conn.execute(
            text(
                """
                SELECT COALESCE(
                    (SELECT inbox_id FROM inbox_dedupe_keys WHERE user_id = :u AND key = :e),
                    (SELECT id FROM inbox WHERE source_id = :s AND external_id = :e)
                )
                """
            ),
            {"u": user_id, "s": source_id, "e": eid},
        ).scalar()
    return Ingested(
        inserted=bool(outcome.inserted),
        inbox_id=int(inbox_id) if inbox_id is not None else None,
    )


def has_media(conn: Connection, inbox_id: int) -> bool:
    return (
        conn.execute(
            text("SELECT 1 FROM media_assets WHERE inbox_id = :i LIMIT 1"), {"i": inbox_id}
        ).first()
        is not None
    )


def attach_media(conn: Connection, user_id: int, inbox_id: int, media: list[MediaItem]) -> int:
    """Archivos que llegan DESPUÉS del mensaje: el historial y la bandeja traen el texto con las
    llaves del archivo, y el archivo se baja aparte con el cliente vivo. Idempotente (una fila que
    ya tiene medios no se toca). Al adjuntar, el mensaje vuelve a la cola de extracción: su
    contenido cambió (hay una imagen que OCR-ear y leer). Devuelve cuántos archivos adjuntó."""
    if not media or has_media(conn, inbox_id):
        return 0
    blobs = [
        MediaBlob(
            sha256=m.sha256,
            content_type=m.content_type,
            filename=m.filename,
            size=m.size,
            data_b64=m.data_b64,
        )
        for m in media
    ]
    persist_media(conn, user_id, inbox_id, decode_media(blobs))
    invalidate_for_reprocess(conn, user_id, [inbox_id])
    _log.info("whatsapp.media.attached", user_id=user_id, inbox_id=inbox_id, files=len(media))
    return len(media)
