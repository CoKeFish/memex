"""CRUD HTTP de `filter_rules` para el dashboard (gestión de filtros pre-ingest).

Espeja el CLI `memex-filters` pero por HTTP y acotado al dueño (`user_id`). Reusa los helpers de
`memex.core.filters` (create/list/get/update/delete) — única fuente de la lógica SQL. Las reglas
filtran ANTES de ingestar: bloquear un remitente corta los PRÓXIMOS mensajes, no los ya recibidos.
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from memex.api.auth import current_user_id
from memex.api.schemas import (
    FilterRuleCreate,
    FilterRuleInfo,
    FilterRuleList,
    FilterRuleUpdate,
)
from memex.core import filters
from memex.db import connection

router = APIRouter(prefix="/filters", tags=["filters"])

UserID = Annotated[int, Depends(current_user_id)]


@router.get("", response_model=FilterRuleList)
async def list_filters(
    user_id: UserID,
    source_type: str | None = Query(default=None),
    source_id: int | None = Query(default=None),
) -> dict[str, Any]:
    with connection() as conn:
        rules = filters.list_rules(
            conn, user_id=user_id, source_type=source_type, source_id=source_id
        )
    return {"items": [r.model_dump() for r in rules]}


@router.post("", response_model=FilterRuleInfo)
async def create_filter(user_id: UserID, body: FilterRuleCreate) -> dict[str, Any]:
    with connection() as conn:
        new_id = filters.create_rule(
            conn,
            user_id=user_id,
            source_type=body.source_type,
            source_id=body.source_id,
            scope=body.scope,
            action=body.action,
            priority=body.priority,
            enabled=body.enabled,
        )
        rule = filters.get_rule(conn, new_id, user_id=user_id)
    assert rule is not None  # recién insertada
    return rule.model_dump()


@router.patch("/{rule_id}", response_model=FilterRuleInfo)
async def update_filter(rule_id: int, user_id: UserID, body: FilterRuleUpdate) -> dict[str, Any]:
    with connection() as conn:
        ok = filters.update_rule(
            conn,
            rule_id,
            user_id=user_id,
            scope=body.scope,
            action=body.action,
            priority=body.priority,
            enabled=body.enabled,
        )
        rule = filters.get_rule(conn, rule_id, user_id=user_id) if ok else None
    if rule is None:
        raise HTTPException(status_code=404, detail="not found")
    return rule.model_dump()


@router.delete("/{rule_id}")
async def delete_filter(rule_id: int, user_id: UserID) -> dict[str, bool]:
    with connection() as conn:
        ok = filters.delete_rule(conn, rule_id, user_id=user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"deleted": True}
