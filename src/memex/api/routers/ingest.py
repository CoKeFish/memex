from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.ingest_service import (
    ingest_one_record,
    ingest_records,
    resolve_source_type,
    to_source_record,
)
from memex.api.schemas import (
    IngestBatchRequest,
    IngestBatchResponse,
    IngestRequest,
    IngestResponse,
)
from memex.core import filters
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/ingest", tags=["ingest"])

UserID = Annotated[int, Depends(current_user_id)]
DryRun = Annotated[str | None, Header(alias="X-Dry-Run")]

_log = get_logger("memex.ingest")


def _dry_run_outcome(user_id: int, body: IngestRequest) -> dict[str, Any]:
    """Valida (sin escribir) si el record entraría: ownership → filtros → duplicado.

    Replica la decisión real de `ingest_one_record` salvo el insert, para que el dry-run del
    dashboard prometa lo mismo que confirmará: `reason` ∈ {filtered, duplicate, None}.
    """
    with connection() as conn:
        owner = conn.execute(
            text("SELECT user_id FROM sources WHERE id = :sid"),
            {"sid": body.source_id},
        ).scalar()
        if owner != user_id:
            raise HTTPException(status_code=404, detail="source not found")
        source_type = resolve_source_type(conn, body.source_id)
        rules = filters.load_active_rules(
            conn, user_id=user_id, source_type=source_type, source_id=body.source_id
        )
        kept, _drops = filters.apply(
            [to_source_record(body)],
            rules,
            source_id=body.source_id,
            source_type=source_type,
        )
        validations = {"source_ownership": "ok"}
        if not kept:
            return {"would_insert": False, "reason": "filtered", "validations": validations}
        exists = conn.execute(
            text("SELECT 1 FROM inbox WHERE source_id = :sid AND external_id = :eid"),
            {"sid": body.source_id, "eid": body.external_id},
        ).scalar()
    if exists:
        return {"would_insert": False, "reason": "duplicate", "validations": validations}
    return {"would_insert": True, "reason": None, "validations": validations}


@router.post("", response_model=IngestResponse)
async def ingest_one(
    body: IngestRequest,
    user_id: UserID,
    x_dry_run: DryRun = None,
) -> dict[str, Any]:
    if x_dry_run:
        return _dry_run_outcome(user_id, body)

    _log.info(
        "ingest.received",
        user_id=user_id,
        source_id=body.source_id,
        count=1,
        external_id=body.external_id,
    )
    try:
        with connection() as conn:
            outcome = ingest_one_record(conn, user_id, body)
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
        inserted=1 if outcome.inserted else 0,
        duplicates=1 if outcome.reason == "duplicate" else 0,
        filtered=1 if outcome.reason == "filtered" else 0,
        errors=0,
    )
    return {"inserted": outcome.inserted, "id": outcome.id, "reason": outcome.reason}


@router.post("/batch", response_model=IngestBatchResponse)
async def ingest_batch(body: IngestBatchRequest, user_id: UserID) -> dict[str, int]:
    _log.info(
        "ingest.received",
        user_id=user_id,
        count=len(body.records),
        source_ids=sorted({r.source_id for r in body.records}),
    )
    with connection() as conn:
        counts = ingest_records(conn, user_id, body.records)
    _log.info(
        "ingest.committed",
        user_id=user_id,
        count=len(body.records),
        inserted=counts["inserted"],
        duplicates=counts["duplicates"],
        errors=counts["errors"],
        filtered=counts["filtered"],
    )
    return counts
