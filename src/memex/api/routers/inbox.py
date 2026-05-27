from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import InboxList, InboxRow, InboxStats, StatsBySource
from memex.db import connection

router = APIRouter(prefix="/inbox", tags=["inbox"])

UserID = Annotated[int, Depends(current_user_id)]


@router.get("", response_model=InboxList)
async def list_inbox(
    user_id: UserID,
    source_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    processed: Literal["true", "false", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    where: list[str] = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if source_id is not None:
        where.append("source_id = :sid")
        params["sid"] = source_id
    if since is not None:
        where.append("occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("occurred_at < :until")
        params["until"] = until
    if processed == "true":
        where.append("processed_at IS NOT NULL")
    elif processed == "false":
        where.append("processed_at IS NULL")
    if cursor is not None:
        where.append("id > :cur")
        params["cur"] = cursor

    sql = f"""
        SELECT id, source_id, external_id, occurred_at, received_at,
               payload, processed_at, process_error, attempts
        FROM inbox
        WHERE {" AND ".join(where)}
        ORDER BY id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [dict(r) for r in rows]
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/stats", response_model=InboxStats)
async def stats(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT source_id,
                           COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE processed_at IS NULL) AS pending,
                           COUNT(*) FILTER (WHERE process_error IS NOT NULL) AS errored
                    FROM inbox
                    WHERE user_id = :uid
                    GROUP BY source_id
                    ORDER BY source_id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    sources = {
        r["source_id"]: StatsBySource(total=r["total"], pending=r["pending"], errored=r["errored"])
        for r in rows
    }
    return {"sources": sources}


@router.get("/{inbox_id}", response_model=InboxRow)
async def get_inbox(inbox_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT id, source_id, external_id, occurred_at, received_at,
                           payload, processed_at, process_error, attempts
                    FROM inbox
                    WHERE id = :id AND user_id = :uid
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return dict(row)
