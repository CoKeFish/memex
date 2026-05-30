import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import Connection, text

from memex.api.auth import current_user_id
from memex.api.object_store import get_object_store
from memex.api.schemas import (
    IngestBatchRequest,
    IngestBatchResponse,
    IngestRequest,
    IngestResponse,
)
from memex.core import filters
from memex.core.inbox import insert_record
from memex.core.media import insert_media_asset
from memex.core.source import MediaBlob, SourceRecord
from memex.db import connection
from memex.logging import get_logger
from memex.storage import object_key_for

router = APIRouter(prefix="/ingest", tags=["ingest"])

UserID = Annotated[int, Depends(current_user_id)]
DryRun = Annotated[str | None, Header(alias="X-Dry-Run")]

_log = get_logger("memex.ingest")


def _to_source_record(req: IngestRequest) -> SourceRecord:
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
class _DecodedMedia:
    """Un blob ya decodificado y validado server-side: sha256 y size RECOMPUTADOS de los bytes."""

    content_type: str
    filename: str | None
    data: bytes
    sha256: str


def _decode_media(media: list[MediaBlob]) -> list[_DecodedMedia]:
    """Decodifica base64 y RECOMPUTA sha256/size de los bytes (no confía en el cliente).

    Levanta `ValueError` si algún base64 es inválido — el caller lo trata como record fallido
    ANTES de insertar el inbox (atomicidad: nunca un inbox huérfano sin su media). Recomputar el
    sha256 garantiza content-addressing real (un cliente que mienta no puede pisar el blob de otro
    mensaje ni romper el dedup).
    """
    out: list[_DecodedMedia] = []
    for blob in media:
        try:
            data = base64.b64decode(blob.data_b64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise ValueError(f"invalid base64 in media (filename={blob.filename!r}): {e}") from e
        out.append(
            _DecodedMedia(
                content_type=blob.content_type,
                filename=blob.filename,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
    return out


def _persist_media(
    conn: Connection, user_id: int, inbox_id: int, media: list[_DecodedMedia]
) -> None:
    """Sube cada blob (ya decodificado) a MinIO content-addressed y registra la referencia.

    Se llama SOLO tras un insert de inbox exitoso (necesita el `inbox_id`). El `put` va ANTES del
    insert de media: si MinIO falla, propaga → la tx del batch hace rollback (ni inbox ni media) y
    el runner re-fetchea (idempotente). PDF se almacena pero queda `skipped` (no se OCR-ea en este
    slice). El object storage se construye lazy (solo si hay media).
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
            ocr_status="skipped" if m.content_type == "application/pdf" else "pending",
        )


def _resolve_source_type(conn: Connection, source_id: int) -> str | None:
    """Lookup `sources.type` for a given source_id. None if not found."""
    row = conn.execute(
        text("SELECT type FROM sources WHERE id = :sid"),
        {"sid": source_id},
    ).scalar()
    return str(row) if row is not None else None


@router.post("", response_model=IngestResponse)
async def ingest_one(
    body: IngestRequest,
    user_id: UserID,
    x_dry_run: DryRun = None,
) -> dict[str, Any]:
    if x_dry_run:
        with connection() as conn:
            owner = conn.execute(
                text("SELECT user_id FROM sources WHERE id = :sid"),
                {"sid": body.source_id},
            ).scalar()
        if owner != user_id:
            raise HTTPException(status_code=404, detail="source not found")
        return {"would_insert": True, "validations": {"source_ownership": "ok"}}

    _log.info(
        "ingest.received",
        user_id=user_id,
        source_id=body.source_id,
        count=1,
        external_id=body.external_id,
    )
    try:
        with connection() as conn:
            source_type = _resolve_source_type(conn, body.source_id)
            rules = filters.load_active_rules(
                conn,
                user_id=user_id,
                source_type=source_type,
                source_id=body.source_id,
            )
            kept, drops = filters.apply(
                [_to_source_record(body)],
                rules,
                source_id=body.source_id,
                source_type=source_type,
            )
            if not kept:
                _log.info(
                    "ingest.committed",
                    user_id=user_id,
                    source_id=body.source_id,
                    inserted=0,
                    duplicates=0,
                    errors=0,
                    filtered=sum(drops.values()),
                )
                return {"inserted": False, "id": None, "reason": "filtered"}
            decoded_media = _decode_media(kept[0].media)  # valida base64 ANTES de insertar
            result = insert_record(
                conn,
                user_id=user_id,
                source_id=body.source_id,
                record=kept[0],
            )
            if result.inserted and result.id is not None:
                _persist_media(conn, user_id, result.id, decoded_media)
    except ValueError as e:
        _log.warning(
            "ingest.committed",
            user_id=user_id,
            source_id=body.source_id,
            inserted=0,
            duplicates=0,
            errors=1,
            reason=str(e),
        )
        raise HTTPException(status_code=404, detail=str(e)) from e
    _log.info(
        "ingest.committed",
        user_id=user_id,
        source_id=body.source_id,
        inserted=1 if result.inserted else 0,
        duplicates=0 if result.inserted else 1,
        errors=0,
    )
    return {"inserted": result.inserted, "id": result.id, "reason": result.reason}


@router.post("/batch", response_model=IngestBatchResponse)
async def ingest_batch(body: IngestBatchRequest, user_id: UserID) -> dict[str, int]:
    _log.info(
        "ingest.received",
        user_id=user_id,
        count=len(body.records),
        source_ids=sorted({r.source_id for r in body.records}),
    )
    inserted = duplicates = errors = filtered = 0
    with connection() as conn:
        # Cache per (source_id) — lookup source_type once, load rules once.
        type_cache: dict[int, str | None] = {}
        rules_cache: dict[int, list[filters.FilterRule]] = {}

        # Group records by source_id so apply() can batch the drop counter
        # per source (the structlog event aggregates by rule_id).
        by_source: dict[int, list[IngestRequest]] = {}
        for req in body.records:
            by_source.setdefault(req.source_id, []).append(req)

        for source_id, reqs in by_source.items():
            if source_id not in type_cache:
                type_cache[source_id] = _resolve_source_type(conn, source_id)
            source_type = type_cache[source_id]
            if source_id not in rules_cache:
                rules_cache[source_id] = filters.load_active_rules(
                    conn,
                    user_id=user_id,
                    source_type=source_type,
                    source_id=source_id,
                )
            records = [_to_source_record(r) for r in reqs]
            kept, drops = filters.apply(
                records,
                rules_cache[source_id],
                source_id=source_id,
                source_type=source_type,
            )
            filtered += sum(drops.values())
            for record in kept:
                try:
                    # Decodificar/validar la media ANTES del insert: un base64 inválido falla el
                    # record completo (errors++) sin dejar un inbox huérfano ni doble-contar.
                    decoded_media = _decode_media(record.media)
                    result = insert_record(
                        conn,
                        user_id=user_id,
                        source_id=source_id,
                        record=record,
                    )
                    if result.inserted:
                        inserted += 1
                        if result.id is not None:
                            _persist_media(conn, user_id, result.id, decoded_media)
                    else:
                        duplicates += 1
                except ValueError:
                    errors += 1
    _log.info(
        "ingest.committed",
        user_id=user_id,
        count=len(body.records),
        inserted=inserted,
        duplicates=duplicates,
        errors=errors,
        filtered=filtered,
    )
    return {
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
        "filtered": filtered,
    }
