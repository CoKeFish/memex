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

router = APIRouter(prefix="/ingest", tags=["ingest"])

UserID = Annotated[int, Depends(current_user_id)]
DryRun = Annotated[str | None, Header(alias="X-Dry-Run")]


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

    try:
        with connection() as conn:
            result = insert_record(
                conn,
                user_id=user_id,
                source_id=body.source_id,
                record=_to_source_record(body),
            )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"inserted": result.inserted, "id": result.id, "reason": result.reason}


@router.post("/batch", response_model=IngestBatchResponse)
async def ingest_batch(body: IngestBatchRequest, user_id: UserID) -> dict[str, int]:
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
    return {"inserted": inserted, "duplicates": duplicates, "errors": errors}
