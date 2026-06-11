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
    where: list[str] = ["c.user_id = :uid", "NOT c.deleted"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if direction is not None:
        where.append("c.direction = :direction")
        params["direction"] = direction
    if currency is not None:
        where.append("c.currency = :currency")
        params["currency"] = currency
    if since is not None:
        where.append("c.occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("c.occurred_at < :until")
        params["until"] = until
    if cursor is not None:
        where.append("c.id > :cur")
        params["cur"] = cursor

    # `evidence` y `source_inbox_ids` no están en el consolidado: se traen de las crudas
    # (`mod_finance_transactions`) — la evidencia de la GANADORA (`winner_transaction_id`) y la
    # unión de mensajes de TODAS las crudas enlazadas. El subquery agrupado por `consolidated_id`
    # no multiplica filas del consolidado; degradan a '' / [] si faltan winner/links.
    sql = f"""
        SELECT c.id, c.direction, c.amount, c.currency, c.category, c.counterparty, c.place,
               c.occurred_at, c.occurred_at_precision, c.description, c.created_at,
               p.name AS place_name, p.formatted_address AS place_address,
               COALESCE(w.evidence, '') AS evidence,
               COALESCE(sib.ids, ARRAY[]::bigint[]) AS source_inbox_ids
        FROM mod_finance_consolidated c
        LEFT JOIN geo_places p ON p.id = c.place_id
        LEFT JOIN mod_finance_transactions w ON w.id = c.winner_transaction_id
        LEFT JOIN (
            SELECT l.consolidated_id, array_agg(DISTINCT s) AS ids
            FROM mod_finance_transaction_links l
            JOIN mod_finance_transactions t ON t.id = l.transaction_id,
                 unnest(t.source_inbox_ids) AS s
            GROUP BY l.consolidated_id
        ) sib ON sib.consolidated_id = c.id
        WHERE {" AND ".join(where)}
        ORDER BY c.id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        d["source_inbox_ids"] = [int(x) for x in d["source_inbox_ids"]]
        items.append(d)
    next_cursor = items[-1]["id"] if len(items) == limit else None
    _log.info("finance.transactions.listed", user_id=user_id, count=len(items), currency=currency)
    return {"items": items, "next_cursor": next_cursor}
