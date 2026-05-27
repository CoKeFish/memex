from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    IngestBatchRequest,
    IngestBatchResponse,
    IngestRequest,
    IngestResponse,
)
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
            result = insert_record(
                conn,
                user_id=user_id,
                source_id=body.source_id,
                record=_to_source_record(body),
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
    inserted = duplicates = errors = 0
    with connection() as conn:
        for req in body.records:
            try:
                result = insert_record(
                    conn,
                    user_id=user_id,
                    source_id=req.source_id,
                    record=_to_source_record(req),
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
    )
    return {"inserted": inserted, "duplicates": duplicates, "errors": errors}
