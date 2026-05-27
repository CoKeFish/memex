import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.api.auth import current_user_id
from memex.api.schemas import CheckpointBody, SourceCreate, SourceRow
from memex.core import checkpoint
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/sources", tags=["sources"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.sources")


def _assert_owns_source(conn: Any, user_id: int, source_id: int) -> None:
    owner = conn.execute(
        text("SELECT user_id FROM sources WHERE id = :sid"),
        {"sid": source_id},
    ).scalar()
    if owner != user_id:
        raise HTTPException(status_code=404, detail="source not found")


@router.get("", response_model=list[SourceRow])
async def list_sources(user_id: UserID) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, user_id, name, type, enabled, config, created_at
                    FROM sources WHERE user_id = :uid ORDER BY id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


@router.post("", response_model=SourceRow, status_code=status.HTTP_201_CREATED)
async def create_source(body: SourceCreate, user_id: UserID) -> dict[str, Any]:
    try:
        with connection() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        INSERT INTO sources (user_id, name, type, config)
                        VALUES (:uid, :name, :type, CAST(:cfg AS JSONB))
                        RETURNING id, user_id, name, type, enabled, config, created_at
                        """
                    ),
                    {
                        "uid": user_id,
                        "name": body.name,
                        "type": body.type,
                        "cfg": json.dumps(body.config),
                    },
                )
                .mappings()
                .first()
            )
    except IntegrityError as e:
        raise HTTPException(
            status_code=409, detail="source with that name already exists for this user"
        ) from e
    assert row is not None
    _log.info(
        "sources.created",
        user_id=user_id,
        source_id=row["id"],
        name=row["name"],
        source_type=row["type"],
    )
    return dict(row)


@router.post("/ensure", response_model=SourceRow)
async def ensure_source(body: SourceCreate, user_id: UserID) -> dict[str, Any]:
    """Get-or-create idempotente por (user_id, name).

    Si la fuente ya existe para este usuario con ese nombre, la devuelve sin
    tocarla. Si no existe, la crea con `type` y `config` provistos.
    """
    with connection() as conn:
        existing = (
            conn.execute(
                text(
                    """
                    SELECT id, user_id, name, type, enabled, config, created_at
                    FROM sources WHERE user_id = :uid AND name = :name
                    """
                ),
                {"uid": user_id, "name": body.name},
            )
            .mappings()
            .first()
        )
        if existing is not None:
            _log.info(
                "sources.ensured",
                user_id=user_id,
                source_id=existing["id"],
                name=existing["name"],
                source_type=existing["type"],
                action="existed",
            )
            return dict(existing)
        row = (
            conn.execute(
                text(
                    """
                    INSERT INTO sources (user_id, name, type, config)
                    VALUES (:uid, :name, :type, CAST(:cfg AS JSONB))
                    RETURNING id, user_id, name, type, enabled, config, created_at
                    """
                ),
                {
                    "uid": user_id,
                    "name": body.name,
                    "type": body.type,
                    "cfg": json.dumps(body.config),
                },
            )
            .mappings()
            .first()
        )
    assert row is not None
    _log.info(
        "sources.ensured",
        user_id=user_id,
        source_id=row["id"],
        name=row["name"],
        source_type=row["type"],
        action="created",
    )
    return dict(row)


@router.get("/{source_id}/checkpoint")
async def get_checkpoint(source_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        cur = checkpoint.get_cursor(conn, source_id)
    return {"cursor": cur}


@router.put("/{source_id}/checkpoint")
async def put_checkpoint(source_id: int, body: CheckpointBody, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns_source(conn, user_id, source_id)
        checkpoint.save_cursor(conn, source_id, body.cursor)
    _log.info(
        "sources.checkpoint.updated",
        user_id=user_id,
        source_id=source_id,
    )
    return {"cursor": body.cursor}
