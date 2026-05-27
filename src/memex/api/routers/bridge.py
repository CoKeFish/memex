"""Bridge — surface única para ingestors externos.

Tres endpoints bajo /bridge/plugins/{plugin_name}/:

- POST  /state   — get-or-create del source asociado al plugin; devuelve
                   `{source_id, cursor, created}`. Es la operación que el
                   cliente externo hace primero para conocer su estado.
- PUT   /cursor  — actualiza el checkpoint persistido del plugin.
- POST  /ingest  — persiste un batch de records. El `source_id` viene del
                   URL, no del payload — los records pasan SourceRecord-shaped
                   sin esa columna.

Todas las operaciones loggean eventos estructurados (`bridge.state.fetched`,
`bridge.cursor.updated`, `bridge.ingest.received`, `bridge.ingest.committed`)
con `plugin`, `user_id`, `source_id` y counts para que el audit log sirva
para correlacionar con lo que el cliente externo cree haber enviado.

Restricción: `plugin_name` debe coincidir con `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`
— validado a nivel de router para impedir creación accidental por path
malformado.
"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import Connection, text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    BridgeCursorRequest,
    BridgeIngestStats,
    BridgePluginIngestRequest,
    BridgeStateRequest,
    BridgeStateResponse,
)
from memex.core import checkpoint
from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/bridge", tags=["bridge"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.bridge")

_PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _validate_plugin_name(name: str) -> None:
    if not _PLUGIN_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail="plugin_name must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$",
        )


def _ensure_source(
    conn: Connection, *, user_id: int, name: str, source_type: str
) -> tuple[int, bool]:
    """Get-or-create por (user_id, name). Devuelve (source_id, fue_creado)."""
    existing = conn.execute(
        text("SELECT id, type FROM sources WHERE user_id = :uid AND name = :name"),
        {"uid": user_id, "name": name},
    ).first()
    if existing is not None:
        return int(existing[0]), False
    new_id = conn.execute(
        text(
            "INSERT INTO sources (user_id, name, type, config) "
            "VALUES (:uid, :name, :type, '{}'::jsonb) RETURNING id"
        ),
        {"uid": user_id, "name": name, "type": source_type},
    ).scalar()
    assert new_id is not None
    return int(new_id), True


def _resolve_source_id(conn: Connection, user_id: int, name: str) -> int:
    sid = conn.execute(
        text("SELECT id FROM sources WHERE user_id = :uid AND name = :name"),
        {"uid": user_id, "name": name},
    ).scalar()
    if sid is None:
        raise HTTPException(
            status_code=404,
            detail=f"plugin {name!r} not registered; call POST /bridge/plugins/{name}/state first",
        )
    return int(sid)


@router.post("/plugins/{plugin_name}/state", response_model=BridgeStateResponse)
async def plugin_state(
    plugin_name: str,
    body: BridgeStateRequest,
    user_id: UserID,
) -> dict[str, Any]:
    _validate_plugin_name(plugin_name)
    with connection() as conn:
        source_id, created = _ensure_source(
            conn, user_id=user_id, name=plugin_name, source_type=body.source_type
        )
        cursor = checkpoint.get_cursor(conn, source_id)
    _log.info(
        "bridge.state.fetched",
        plugin=plugin_name,
        user_id=user_id,
        source_id=source_id,
        action="created" if created else "existed",
        source_type=body.source_type,
        has_cursor=cursor is not None,
    )
    return {"source_id": source_id, "cursor": cursor, "created": created}


@router.put("/plugins/{plugin_name}/cursor", response_model=BridgeStateResponse)
async def plugin_cursor(
    plugin_name: str,
    body: BridgeCursorRequest,
    user_id: UserID,
) -> dict[str, Any]:
    _validate_plugin_name(plugin_name)
    with connection() as conn:
        source_id = _resolve_source_id(conn, user_id, plugin_name)
        checkpoint.save_cursor(conn, source_id, body.cursor)
    _log.info(
        "bridge.cursor.updated",
        plugin=plugin_name,
        user_id=user_id,
        source_id=source_id,
    )
    return {"source_id": source_id, "cursor": body.cursor, "created": False}


@router.post("/plugins/{plugin_name}/ingest", response_model=BridgeIngestStats)
async def plugin_ingest(
    plugin_name: str,
    body: BridgePluginIngestRequest,
    user_id: UserID,
) -> dict[str, Any]:
    _validate_plugin_name(plugin_name)
    with connection() as conn:
        source_id = _resolve_source_id(conn, user_id, plugin_name)
        _log.info(
            "bridge.ingest.received",
            plugin=plugin_name,
            user_id=user_id,
            source_id=source_id,
            count=len(body.records),
        )
        inserted = duplicates = errors = 0
        for req in body.records:
            try:
                result = insert_record(
                    conn,
                    user_id=user_id,
                    source_id=source_id,
                    record=SourceRecord(
                        external_id=req.external_id,
                        occurred_at=req.occurred_at,
                        payload=req.payload,
                        dedupe_keys=req.dedupe_keys,
                    ),
                )
                if result.inserted:
                    inserted += 1
                else:
                    duplicates += 1
            except ValueError:
                errors += 1
    _log.info(
        "bridge.ingest.committed",
        plugin=plugin_name,
        user_id=user_id,
        source_id=source_id,
        inserted=inserted,
        duplicates=duplicates,
        errors=errors,
    )
    return {
        "source_id": source_id,
        "inserted": inserted,
        "duplicates": duplicates,
        "errors": errors,
    }
