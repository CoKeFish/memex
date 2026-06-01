from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import FinanceExpenseList
from memex.db import connection
from memex.logging import get_logger

router = APIRouter(prefix="/finance", tags=["finance"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.finance")


@router.get("/expenses", response_model=FinanceExpenseList)
async def list_expenses(
    user_id: UserID,
    currency: str | None = None,
    since: date | None = None,
    until: date | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    """Lista los gastos extraídos por el módulo finance (tabla `mod_finance_expenses`).

    Devuelve filas crudas; el dashboard agrega en el cliente (KPIs, por mes/categoría/comercio). La
    paginación es por cursor (`id > :cur`) igual que `/inbox`; el front itera hasta traer todo. Los
    filtros `currency`/`since`/`until` espejan el patrón de `/inbox` y dan margen futuro.
    """
    where: list[str] = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if currency is not None:
        where.append("currency = :currency")
        params["currency"] = currency
    if since is not None:
        where.append("occurred_on >= :since")
        params["since"] = since
    if until is not None:
        where.append("occurred_on < :until")
        params["until"] = until
    if cursor is not None:
        where.append("id > :cur")
        params["cur"] = cursor

    sql = f"""
        SELECT id, amount, currency, category, merchant, occurred_on,
               description, evidence, source_inbox_ids, created_at
        FROM mod_finance_expenses
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
    _log.info("finance.expenses.listed", user_id=user_id, count=len(items), currency=currency)
    return {"items": items, "next_cursor": next_cursor}
