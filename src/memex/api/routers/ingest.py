from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import Connection, text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    IngestBatchRequest,
    IngestBatchResponse,
    IngestRequest,
    IngestResponse,
)
from memex.core import filters
from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection
from memex.logging import get_logger

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
            result = insert_record(
                conn,
                user_id=user_id,
                source_id=body.source_id,
                record=kept[0],
            )
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
                    result = insert_record(
                        conn,
                        user_id=user_id,
                        source_id=source_id,
                        record=record,
                    )
                    if result.inserted:
                        inserted += 1
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
    return {"inserted": inserted, "duplicates": duplicates, "errors": errors}
