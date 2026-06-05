from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import FinanceTransactionList
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/finance", tags=["finance"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.finance")


@router.get("/transactions", response_model=FinanceTransactionList)
async def list_transactions(
    user_id: UserID,
    direction: str | None = Query(default=None, description="ingreso | egreso"),
    currency: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    """Lista las transacciones CONSOLIDADAS del módulo finance (`mod_finance_consolidated`, no
    borradas) — la vista deduplicada (un cargo reportado por dos fuentes aparece UNA vez).

    Devuelve filas canónicas; el dashboard agrega en el cliente (KPIs, por mes/categoría). La
    paginación es por cursor (`id > :cur`) igual que `/inbox`. Los filtros `direction`/`currency`/
    `since`/`until` espejan el patrón de `/inbox`.
    """
    where: list[str] = ["user_id = :uid", "NOT deleted"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if direction is not None:
        where.append("direction = :direction")
        params["direction"] = direction
    if currency is not None:
        where.append("currency = :currency")
        params["currency"] = currency
    if since is not None:
        where.append("occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("occurred_at < :until")
        params["until"] = until
    if cursor is not None:
        where.append("id > :cur")
        params["cur"] = cursor

    sql = f"""
        SELECT id, direction, amount, currency, category, counterparty, place,
               occurred_at, occurred_at_precision, description, created_at
        FROM mod_finance_consolidated
        WHERE {" AND ".join(where)}
        ORDER BY id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        items.append(d)
    next_cursor = items[-1]["id"] if len(items) == limit else None
    _log.info("finance.transactions.listed", user_id=user_id, count=len(items), currency=currency)
    return {"items": items, "next_cursor": next_cursor}
