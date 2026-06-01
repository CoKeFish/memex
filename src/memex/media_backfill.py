"""Backfill de adjuntos: re-baja por IMAP los bytes de adjuntos de correos YA ingeridos que NO
tienen `media_asset`, y los persiste (MinIO + `media_assets` pending) contra el inbox existente.

Por qué hace falta: `persist_media` solo corre en el INSERT de un inbox nuevo (`api/ingest_service`)
y re-traer un correo existente es idempotente (duplicado → no crea media). Acá re-bajamos por UID
(del `external_id`) con `extract_media` FORZADO, sin tocar el inbox ni sus filas derivadas
(resumen/extracción). Server-side (no es un ingestor-plugin): orquesta primitivas IMAP + el
`persist_media` del borde de ingest (`api`), por eso vive a nivel `memex`, no bajo `ingestors/`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, text

from memex.api.ingest_service import decode_media, persist_media
from memex.db import connection
from memex.ingestors.imap.client import ImapClient
from memex.ingestors.imap.config import ImapConfig, ImapConfigError
from memex.ingestors.imap.parser import parse_email_message
from memex.logging import get_logger

_log = get_logger("memex.media_backfill")


@dataclass
class BackfillStats:
    """Resumen de una corrida de backfill de media."""

    targets: int = 0  # inbox elegibles (adjuntos declarados, sin media, fuente imap)
    messages: int = 0  # mensajes re-bajados con media extraíble
    assets_created: int = 0  # filas media_assets nuevas (pending)
    skipped: int = 0  # no-imap / uidvalidity cambió / mensaje no hallado / sin media extraíble
    errors: int = 0


@dataclass(frozen=True)
class _Target:
    inbox_id: int
    source_id: int
    external_id: str
    folder: str


def _load_targets(user_id: int, inbox_ids: list[int]) -> list[_Target]:
    """inbox del user, de fuentes imap, con adjuntos declarados y SIN media_assets (idempotente)."""
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT i.id, i.source_id, i.external_id,
                           COALESCE(i.payload->>'folder', 'INBOX') AS folder
                    FROM inbox i
                    JOIN sources s ON s.id = i.source_id
                    WHERE i.user_id = :uid
                      AND i.id = ANY(:iids)
                      AND s.type = 'imap'
                      AND jsonb_typeof(i.payload->'attachments') = 'array'
                      AND jsonb_array_length(i.payload->'attachments') > 0
                      AND NOT EXISTS (SELECT 1 FROM media_assets m WHERE m.inbox_id = i.id)
                    ORDER BY i.id
                    """
                ),
                {"uid": user_id, "iids": inbox_ids},
            )
            .mappings()
            .all()
        )
    return [
        _Target(int(r["id"]), int(r["source_id"]), str(r["external_id"]), str(r["folder"]))
        for r in rows
    ]


def _source_config(source_id: int) -> dict[str, Any]:
    with connection() as conn:
        row = conn.execute(
            text("SELECT config FROM sources WHERE id = :sid"), {"sid": source_id}
        ).scalar()
    return dict(row) if isinstance(row, dict) else {}


def _parse_external_id(external_id: str) -> tuple[int, int] | None:
    """`imap:{server}:{uidvalidity}:{uid}` → (uidvalidity, uid), o None si no matchea."""
    parts = external_id.split(":")
    if len(parts) < 4 or parts[0] != "imap":
        return None
    try:
        return int(parts[-2]), int(parts[-1])
    except ValueError:
        return None


def _media_count(conn: Connection, inbox_id: int) -> int:
    return int(
        conn.execute(
            text("SELECT count(*) FROM media_assets WHERE inbox_id = :i"), {"i": inbox_id}
        ).scalar()
        or 0
    )


def _record_media(cfg: ImapConfig, mailmsg: Any, folder: str, uidvalidity: int) -> list[Any]:
    """Re-parsea el mensaje con `extract_media=True` y devuelve sus `MediaBlob` (espeja
    `ImapSource._mailmsg_to_record`, pero solo nos interesa `record.media`)."""
    uid_str = getattr(mailmsg, "uid", None) or "0"
    try:
        uid = int(uid_str)
    except ValueError:
        uid = 0
    flags = list(getattr(mailmsg, "flags", ()) or ())
    size_bytes = int(getattr(mailmsg, "size", 0) or 0)
    internaldate = getattr(mailmsg, "date", None)
    if internaldate is None:
        internaldate = datetime.now(UTC)
    elif internaldate.tzinfo is None:
        internaldate = internaldate.replace(tzinfo=UTC)
    record = parse_email_message(
        mailmsg.obj,
        server=cfg.server,
        folder=folder,
        uidvalidity=uidvalidity,
        uid=uid,
        internaldate=internaldate,
        flags=flags,
        size_bytes=size_bytes,
        max_body_bytes=cfg.max_body_bytes,
        fetch_body=cfg.fetch_body,
        extract_media=True,
        max_attachment_bytes=cfg.max_attachment_bytes,
    )
    return list(record.media)


def _backfill_one(
    user_id: int, client: ImapClient, cfg: ImapConfig, target: _Target, stats: BackfillStats
) -> None:
    parsed = _parse_external_id(target.external_id)
    if parsed is None:
        stats.skipped += 1
        _log.warning("backfill.bad_external_id", inbox_id=target.inbox_id, ext=target.external_id)
        return
    uidvalidity, uid = parsed
    current = client.folder_uidvalidity(target.folder)
    if current != uidvalidity:
        # El UID ya no es válido (el folder se reindexó) → no se puede re-bajar de forma fiable.
        stats.skipped += 1
        _log.warning(
            "backfill.uidvalidity_changed",
            inbox_id=target.inbox_id,
            stored=uidvalidity,
            current=current,
        )
        return
    messages = list(client.fetch_uids(target.folder, [uid]))
    if not messages:
        stats.skipped += 1
        _log.warning("backfill.message_not_found", inbox_id=target.inbox_id, uid=uid)
        return
    media = _record_media(cfg, messages[0], target.folder, uidvalidity)
    if not media:
        stats.skipped += 1
        _log.info("backfill.no_extractable_media", inbox_id=target.inbox_id)
        return
    decoded = decode_media(media)
    with connection() as conn:
        before = _media_count(conn, target.inbox_id)
        persist_media(conn, user_id, target.inbox_id, decoded)
        created = _media_count(conn, target.inbox_id) - before
    stats.messages += 1
    stats.assets_created += created
    _log.info("backfill.message_ok", inbox_id=target.inbox_id, assets_created=created)


def backfill_inbox_media(user_id: int, inbox_ids: list[int]) -> BackfillStats:
    """Re-baja y persiste los adjuntos (pending) de los inbox dados que aún no tienen media.

    Best-effort por mensaje y por fuente: un fallo se loguea + cuenta y NO frena el resto. El OCR
    es un paso aparte (worker `memex-ocr` / stage `ocr` del reproceso) que consume los `pending`.
    """
    stats = BackfillStats()
    if not inbox_ids:
        return stats
    targets = _load_targets(user_id, inbox_ids)
    stats.targets = len(targets)
    if not targets:
        _log.info("backfill.no_targets", user_id=user_id, requested=len(inbox_ids))
        return stats

    by_source: dict[int, list[_Target]] = defaultdict(list)
    for t in targets:
        by_source[t.source_id].append(t)

    for source_id, group in by_source.items():
        try:
            raw_cfg = {**_source_config(source_id), "extract_media": True}
            cfg = ImapConfig.from_source_config(raw_cfg)
        except ImapConfigError as e:
            stats.errors += len(group)
            _log.error("backfill.config_invalid", source_id=source_id, error=str(e))
            continue
        try:
            with ImapClient(cfg) as client:
                for target in group:
                    try:
                        _backfill_one(user_id, client, cfg, target, stats)
                    except Exception as e:  # best-effort por mensaje
                        stats.errors += 1
                        _log.error(
                            "backfill.message_failed",
                            inbox_id=target.inbox_id,
                            exc_type=type(e).__name__,
                            exc_msg=str(e),
                        )
        except Exception as e:  # login/conexión de la fuente
            stats.errors += len(group)
            _log.error(
                "backfill.source_failed",
                source_id=source_id,
                exc_type=type(e).__name__,
                exc_msg=str(e),
            )

    _log.info("backfill.done", user_id=user_id, **asdict(stats))
    return stats
