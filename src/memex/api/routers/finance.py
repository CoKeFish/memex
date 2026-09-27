from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    FinancePendingList,
    FinanceRubroList,
    FinanceRubroRemove,
    FinanceRubroSet,
    FinanceRubroSetOut,
    FinanceSubscriptionList,
    FinanceTransactionList,
)
from memex.core.source import SourceKind
from memex.db import connection
from memex.logging import get_logger
from memex.modules.finance import rubros
from memex.modules.finance.manual import pending_payments
from memex.modules.finance.subscriptions import list_subscriptions, monthly_commitment
from memex.sources import kind_for_type, kind_types

#: Tipos de fuente que son un DOCUMENTO subido (extracto del banco, PDF de la carpeta). Un mes
#: sin ninguno de estos solo tiene lo que llegó por correo o notificación: está incompleto.
_DOCUMENT_TYPES = [t for t in kind_types() if kind_for_type(t) is SourceKind.DOCUMENTOS]

router = APIRouter(prefix="/finance", tags=["finance"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.finance")


@router.get("/pending", response_model=FinancePendingList)
async def list_pending(
    user_id: UserID,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Pagos fallidos o pendientes SIN un pago exitoso posterior a la misma contraparte (lo que
    falta regularizar). Vista determinista; reemplaza a los asuntos «Pagos <servicio>»."""
    with connection() as conn:
        rows = pending_payments(conn, user_id, resolve_days=days, limit=limit)
    return {"items": rows, "resolve_days": days}


@router.get("/subscriptions", response_model=FinanceSubscriptionList)
async def list_subs(
    user_id: UserID,
    include_inactive: bool = Query(default=False, description="incluye canceladas y descartadas"),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Los cobros que se repiten (suscripciones detectadas por cadencia), lo más próximo a
    renovar primero, con el compromiso mensual por moneda. El detector vive en el módulo
    (`memex-finance subs detect`); esto solo lee."""
    with connection() as conn:
        items = list_subscriptions(conn, user_id, include_inactive=include_inactive, limit=limit)
        commitment = monthly_commitment(conn, user_id)
    return {"items": items, "monthly_commitment": commitment}


@router.get("/transactions", response_model=FinanceTransactionList)
async def list_transactions(
    user_id: UserID,
    direction: str | None = Query(default=None, description="ingreso | egreso"),
    status: str = Query(
        default="completada",
        description=(
            "completada (default: solo plata que se movió) | fallida | pendiente | all (todas)"
        ),
    ),
    kind: str | None = Query(default=None, description="pago | reembolso"),
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

    `status` (0090) filtra por defecto a `completada`: la plata que efectivamente se movió. Un
    consumidor que quiera ver los cobros fallidos o lo pendiente por pagar lo pide explícito
    (`status=fallida|pendiente|all`). `kind` separa pagos de reembolsos (un reembolso es `ingreso`
    pero descuenta del gasto, no suma a lo que entró; el cliente lo trata así).
    """
    where: list[str] = ["c.user_id = :uid", "NOT c.deleted"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit, "doc_types": _DOCUMENT_TYPES}

    if direction is not None:
        where.append("c.direction = :direction")
        params["direction"] = direction
    if status != "all":
        where.append("c.status = :status")
        params["status"] = status
    if kind is not None:
        where.append("c.kind = :kind")
        params["kind"] = kind
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
        SELECT c.id, c.direction, c.status, c.kind, c.amount, c.currency, c.category,
               c.counterparty, c.place, c.occurred_at, c.occurred_at_precision, c.description,
               c.created_at,
               p.name AS place_name, p.formatted_address AS place_address,
               COALESCE(w.evidence, '') AS evidence,
               COALESCE(sib.ids, ARRAY[]::bigint[]) AS source_inbox_ids,
               COALESCE(sib.from_document, FALSE) AS from_document
        FROM mod_finance_consolidated c
        LEFT JOIN geo_places p ON p.id = c.place_id
        LEFT JOIN mod_finance_transactions w ON w.id = c.winner_transaction_id
        LEFT JOIN (
            SELECT l.consolidated_id, array_agg(DISTINCT s) AS ids,
                   bool_or(src.type = ANY(:doc_types)) AS from_document
            FROM mod_finance_transaction_links l
            JOIN mod_finance_transactions t ON t.id = l.transaction_id,
                 unnest(t.source_inbox_ids) AS s
            -- LEFT: un mensaje borrado no debe hacer desaparecer la procedencia de la fila.
            LEFT JOIN inbox i ON i.id = s
            LEFT JOIN sources src ON src.id = i.source_id
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


def _rubro_row(r: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(r["id"]),
        "counterparty": str(r["counterparty"]),
        "category": str(r["category"]),
        "source": str(r["source"]),
        "note": str(r.get("note") or ""),
        "updated_at": r["updated_at"],
    }


@router.get("/rubros", response_model=FinanceRubroList)
async def list_rubros(user_id: UserID) -> dict[str, Any]:
    """Reglas de rubro por contraparte: la palabra del dueño (`user`) o lo que el modelo decidió
    en la reclasificación (`llm`, que el dueño puede pisar)."""
    with connection() as conn:
        rows = rubros.list_rules(conn, user_id)
    return {"items": [_rubro_row(r) for r in rows]}


@router.post("/rubros", response_model=FinanceRubroSetOut)
async def set_rubro(user_id: UserID, body: FinanceRubroSet) -> dict[str, Any]:
    """«Este comercio es comida»: guarda la regla y la aplica a TODO lo de esa contraparte,
    pasado y futuro. Devuelve cuántos pagos cambiaron."""
    try:
        with connection() as conn:
            rule = rubros.set_rule(conn, user_id, body.counterparty, body.category, note=body.note)
            assert rule is not None
            applied = rubros.apply_rule(conn, user_id, body.counterparty, body.category)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _log.info(
        "finance.rubro.set",
        user_id=user_id,
        counterparty=body.counterparty,
        category=body.category,
        applied=applied,
    )
    return {"rule": _rubro_row(rule), "applied": applied}


@router.post("/rubros/remove")
async def remove_rubro(user_id: UserID, body: FinanceRubroRemove) -> dict[str, Any]:
    """Quita la regla. Lo ya clasificado no se toca: la regla dejaba de mandar hacia adelante."""
    with connection() as conn:
        ok = rubros.remove_rule(conn, user_id, body.counterparty)
    return {"removed": ok}
