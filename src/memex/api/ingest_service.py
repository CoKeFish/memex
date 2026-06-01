"""Lógica de ingesta server-side reutilizable.

Extraída de `routers/ingest.py` para que TANTO el handler HTTP `/ingest`(`/batch`) COMO el
sink in-process (`memex.api.inprocess_sink`, usado por el endpoint de fetch del dashboard)
compartan exactamente la misma ruta: filtros → decode/validación de media → `insert_record`
→ persistencia de media → contadores. Vive en el paquete `api` (no en `core`) porque depende
del `ObjectStore` de MinIO, que es un detalle del borde de ingest server-side.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass

from sqlalchemy import Connection, text

from memex.api.object_store import get_object_store
from memex.api.schemas import IngestRequest
from memex.core import filters
from memex.core.inbox import insert_record
from memex.core.media import extension_for, insert_media_asset
from memex.core.source import MediaBlob, SourceRecord
from memex.storage import object_key_for


def to_source_record(req: IngestRequest) -> SourceRecord:
    return SourceRecord(
        external_id=req.external_id,
        occurred_at=req.occurred_at,
        payload=req.payload,
        dedupe_keys=req.dedupe_keys,
        media=[
            MediaBlob(
                sha256=m.sha256,
                content_type=m.content_type,
                filename=m.filename,
                size=m.size,
                data_b64=m.data_b64,
            )
            for m in req.media
        ],
    )


@dataclass(frozen=True)
class DecodedMedia:
    """Un blob ya decodificado y validado server-side: sha256 y size RECOMPUTADOS de los bytes."""

    content_type: str
    filename: str | None
    data: bytes
    sha256: str


def decode_media(media: list[MediaBlob]) -> list[DecodedMedia]:
    """Decodifica base64 y RECOMPUTA sha256/size de los bytes (no confía en el cliente).

    Levanta `ValueError` si algún base64 es inválido — el caller lo trata como record fallido
    ANTES de insertar el inbox (atomicidad: nunca un inbox huérfano sin su media). Recomputar el
    sha256 garantiza content-addressing real (un cliente que mienta no puede pisar el blob de otro
    mensaje ni romper el dedup).
    """
    out: list[DecodedMedia] = []
    for blob in media:
        try:
            data = base64.b64decode(blob.data_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"invalid base64 in media (filename={blob.filename!r}): {e}") from e
        out.append(
            DecodedMedia(
                content_type=blob.content_type,
                filename=blob.filename,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return out


def persist_media(conn: Connection, user_id: int, inbox_id: int, media: list[DecodedMedia]) -> None:
    """Sube cada blob (ya decodificado) a MinIO content-addressed y registra la referencia.

    Se llama SOLO tras un insert de inbox exitoso (necesita el `inbox_id`). El `put` va ANTES del
    insert de media: si MinIO falla, propaga → la tx del batch hace rollback (ni inbox ni media) y
    el runner re-fetchea (idempotente). Tanto imágenes como PDFs entran `pending` (el default de
    `insert_media_asset`): el worker `memex-ocr` los procesa — imágenes por visión directa, PDFs
    por capa de texto + visión de imágenes/páginas. El object storage se construye lazy.
    """
    if not media:
        return
    store = get_object_store()
    for m in media:
        key = object_key_for(user_id, m.sha256, m.content_type)
        store.put(key, m.data, content_type=m.content_type)
        insert_media_asset(
            conn,
            user_id=user_id,
            inbox_id=inbox_id,
            sha256=m.sha256,
            object_key=key,
            bucket=store.bucket,
            content_type=m.content_type,
            size_bytes=len(m.data),
            filename=m.filename,
            extension=extension_for(m.filename, m.content_type),
        )


def resolve_source_type(conn: Connection, source_id: int) -> str | None:
    """Lookup `sources.type` for a given source_id. None if not found."""
    row = conn.execute(
        text("SELECT type FROM sources WHERE id = :sid"),
        {"sid": source_id},
    ).scalar()
    return str(row) if row is not None else None


@dataclass(frozen=True)
class SingleIngestOutcome:
    inserted: bool
    id: int | None
    reason: str | None  # "filtered" | "duplicate" | None


def ingest_one_record(conn: Connection, user_id: int, req: IngestRequest) -> SingleIngestOutcome:
    """Ingesta un record: filtros → decode media → insert → persist media.

    Levanta `ValueError` si el source no existe / no pertenece al usuario (el caller lo
    traduce a 404) o si la media trae base64 inválido.
    """
    source_type = resolve_source_type(conn, req.source_id)
    rules = filters.load_active_rules(
        conn, user_id=user_id, source_type=source_type, source_id=req.source_id
    )
    kept, _drops = filters.apply(
        [to_source_record(req)], rules, source_id=req.source_id, source_type=source_type
    )
    if not kept:
        return SingleIngestOutcome(inserted=False, id=None, reason="filtered")
    decoded_media = decode_media(kept[0].media)  # valida base64 ANTES de insertar
    result = insert_record(conn, user_id=user_id, source_id=req.source_id, record=kept[0])
    if result.inserted and result.id is not None:
        persist_media(conn, user_id, result.id, decoded_media)
    return SingleIngestOutcome(inserted=result.inserted, id=result.id, reason=result.reason)


def ingest_records(conn: Connection, user_id: int, reqs: list[IngestRequest]) -> dict[str, int]:
    """Ingesta un batch idempotente. Devuelve {inserted, duplicates, errors, filtered}.

    Cachea source_type + filter_rules por source_id (un lookup por source, no por record).
    Un base64 inválido falla el record completo (errors++) sin dejar inbox huérfano.
    """
    inserted = duplicates = errors = filtered = 0
    type_cache: dict[int, str | None] = {}
    rules_cache: dict[int, list[filters.FilterRule]] = {}

    by_source: dict[int, list[IngestRequest]] = {}
    for req in reqs:
        by_source.setdefault(req.source_id, []).append(req)

    for source_id, source_reqs in by_source.items():
        if source_id not in type_cache:
            type_cache[source_id] = resolve_source_type(conn, source_id)
        source_type = type_cache[source_id]
        if source_id not in rules_cache:
            rules_cache[source_id] = filters.load_active_rules(
                conn, user_id=user_id, source_type=source_type, source_id=source_id
            )
        records = [to_source_record(r) for r in source_reqs]
        kept, drops = filters.apply(
            records, rules_cache[source_id], source_id=source_id, source_type=source_type
        )
        filtered += sum(drops.values())
        for record in kept:
            try:
                decoded_media = decode_media(record.media)
                result = insert_record(conn, user_id=user_id, source_id=source_id, record=record)
                if result.inserted:
                    inserted += 1
                    if result.id is not None:
                        persist_media(conn, user_id, result.id, decoded_media)
                else:
                    duplicates += 1
            except ValueError:
                errors += 1
    return {
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
        "filtered": filtered,
    }
