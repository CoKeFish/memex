"""Operaciones manuales sobre el pago CONSOLIDADO — la vía del agente para corregir/consultar.

Operan sobre `mod_finance_consolidated` (la entidad canónica que leen el dashboard y el API),
no sobre las crudas. Hoy: asociar un lugar del catálogo (`set_place` — el camino intermedio
mientras no existe la app de pings: el agente registra "este pago fue en X") y el detalle con el
lugar resuelto (`show_transaction`). Jerarquía de confianza: si después el seam GPS resuelve un
lugar para alguna cruda del grupo, ese pisa la asociación manual (el ping es la única fuente
fiable de posición).

Validaciones con `ValueError` (mensaje para el agente, no traceback); la CLI las reporta y sale 1.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.geo.places import get_place
from memex.logging import get_logger
from memex.relations.edges import Ref
from memex.relations.graph_writer import delete_vertex

_log = get_logger("memex.modules.finance.manual")


def show_transaction(conn: Connection, user_id: int, consolidated_id: int) -> dict[str, Any] | None:
    """El detalle de un pago consolidado con su lugar resuelto (LEFT JOIN al catálogo), o None si
    no existe (o está tombstoneado por un merge)."""
    row = (
        conn.execute(
            text(
                """
                SELECT c.id, c.direction, c.status, c.kind, c.amount, c.currency, c.category,
                       c.counterparty, c.counterparty_identity_id, c.place, c.occurred_at,
                       c.occurred_at_precision, c.description, c.winner_transaction_id,
                       c.place_id, p.name AS place_name, p.formatted_address AS place_address,
                       c.created_at, c.updated_at
                FROM mod_finance_consolidated c
                LEFT JOIN geo_places p ON p.id = c.place_id
                WHERE c.id = :cid AND c.user_id = :uid AND NOT c.deleted
                """
            ),
            {"cid": consolidated_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    out = dict(row)
    out["amount"] = float(out["amount"])
    return out


def remove_transaction(conn: Connection, user_id: int, consolidated_id: int) -> dict[str, Any]:
    """Borra DEFINITIVAMENTE un pago consolidado y sus transacciones crudas: la vía para deshacer
    un registro (del bot, que registra facturas sin confirmar, o del agente). Físico y no
    tombstone porque finanzas no tiene `deleted_source`: `run_consolidation` resucita `deleted`
    mientras queden crudas. Cascada por FK: links, candidatos de dedup, cargos de suscripción. El
    vértice del grafo sale por `delete_vertex` (chokepoint). Si las crudas vinieron de un
    correo/extracto, reprocesar ESE mensaje lo traería de vuelta. `ValueError` si no existe."""
    detail = show_transaction(conn, user_id, consolidated_id)
    if detail is None:
        raise ValueError(f"el pago consolidado #{consolidated_id} no existe")
    tx_ids = [
        int(r[0])
        for r in conn.execute(
            text(
                "SELECT transaction_id FROM mod_finance_transaction_links "
                "WHERE consolidated_id = :cid AND user_id = :uid ORDER BY transaction_id"
            ),
            {"cid": consolidated_id, "uid": user_id},
        )
    ]
    delete_vertex(conn, user_id, Ref("finance", consolidated_id))
    if tx_ids:
        conn.execute(
            text("DELETE FROM mod_finance_transactions WHERE user_id = :uid AND id = ANY(:ids)"),
            {"uid": user_id, "ids": tx_ids},
        )
    conn.execute(
        text("DELETE FROM mod_finance_consolidated WHERE id = :cid AND user_id = :uid"),
        {"cid": consolidated_id, "uid": user_id},
    )
    _log.info(
        "finance.manual.removed",
        user_id=user_id,
        consolidated_id=consolidated_id,
        transactions=tx_ids,
        amount=str(detail["amount"]),
        counterparty=detail["counterparty"],
    )
    return {
        "removed": consolidated_id,
        "transactions": tx_ids,
        "amount": detail["amount"],
        "currency": detail["currency"],
        "counterparty": detail["counterparty"],
        "occurred_at": detail["occurred_at"],
    }


def set_place(
    conn: Connection, user_id: int, consolidated_id: int, place_id: int | None
) -> dict[str, Any]:
    """Asocia (o limpia, con None) un lugar del catálogo al pago consolidado. Valida que el pago
    y el lugar existan y sean del user. Devuelve el detalle actualizado (`show_transaction`)."""
    if show_transaction(conn, user_id, consolidated_id) is None:
        raise ValueError(f"el pago consolidado #{consolidated_id} no existe")
    if place_id is not None and get_place(conn, user_id, place_id) is None:
        raise ValueError(
            f"el lugar #{place_id} no está en el catálogo (listalos con 'memex-geo places')"
        )
    conn.execute(
        text(
            "UPDATE mod_finance_consolidated SET place_id = :pid, updated_at = NOW() "
            "WHERE id = :cid AND user_id = :uid"
        ),
        {"pid": place_id, "cid": consolidated_id, "uid": user_id},
    )
    _log.info(
        "finance.place_set", user_id=user_id, consolidated_id=consolidated_id, place_id=place_id
    )
    detail = show_transaction(conn, user_id, consolidated_id)
    assert detail is not None  # recién validado arriba, misma tx
    return detail


#: Claves de agregación de `summarize_transactions` → expresión SQL del grupo.
SUMMARY_KEYS: dict[str, str] = {
    "counterparty": "COALESCE(NULLIF(btrim(c.counterparty), ''), '(sin contraparte)')",
    "category": "c.category",
    "month": "to_char(c.occurred_at AT TIME ZONE 'America/Bogota', 'YYYY-MM')",
    "currency": "c.currency",
}


_EGRESOS = "COALESCE(SUM(c.amount) FILTER (WHERE c.direction = 'egreso' AND c.kind = 'pago'), 0)"
_INGRESOS = "COALESCE(SUM(c.amount) FILTER (WHERE c.direction = 'ingreso' AND c.kind = 'pago'), 0)"
_REEMBOLSOS = "COALESCE(SUM(c.amount) FILTER (WHERE c.kind = 'reembolso'), 0)"


def _filters(
    *,
    q: str | None,
    category: str | None,
    direction: str | None,
    status: str | None,
    since: date | None,
    until: date | None,
) -> tuple[list[str], dict[str, Any]]:
    where = ["c.user_id = :uid", "NOT c.deleted"]
    params: dict[str, Any] = {}
    if q:
        where.append("(c.counterparty ILIKE :q OR c.description ILIKE :q OR c.place ILIKE :q)")
        params["q"] = f"%{q}%"
    if category:
        where.append("c.category = :category")
        params["category"] = category
    if direction:
        where.append("c.direction = :direction")
        params["direction"] = direction
    if status and status != "all":
        where.append("c.status = :status")
        params["status"] = status
    if since is not None:
        where.append("c.occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("c.occurred_at < :until")
        params["until"] = until
    return where, params


def list_transactions(
    conn: Connection,
    user_id: int,
    *,
    q: str | None = None,
    category: str | None = None,
    direction: str | None = None,
    status: str | None = "completada",
    since: date | None = None,
    until: date | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Pagos CONSOLIDADOS (la vista deduplicada) filtrados por texto (contraparte/descripción/
    lugar), rubro, sentido, estado (default solo `completada`: plata que se movió; `all` = todos)
    y rango de fechas; los más recientes primero. Entrada del agente para «qué gasté en X».
    Devuelve filas públicas (amount float)."""
    where, params = _filters(
        q=q, category=category, direction=direction, status=status, since=since, until=until
    )
    params.update({"uid": user_id, "limit": limit})
    rows = (
        conn.execute(
            text(
                f"""
                SELECT c.id, c.direction, c.status, c.kind, c.amount, c.currency, c.category,
                       c.counterparty, c.place, c.occurred_at, c.occurred_at_precision,
                       c.description, p.name AS place_name
                FROM mod_finance_consolidated c
                LEFT JOIN geo_places p ON p.id = c.place_id
                WHERE {" AND ".join(where)}
                ORDER BY c.occurred_at DESC, c.id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        out.append(d)
    return out


def pending_payments(
    conn: Connection,
    user_id: int,
    *,
    resolve_days: int = 30,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Pagos FALLIDOS o PENDIENTES (egresos) sin un pago `completada` posterior a la misma
    contraparte dentro de `resolve_days`: lo que falta regularizar. Es la vista determinista que
    reemplaza a los asuntos «Pagos <servicio>» (decisión 2026-09-09: finanzas guarda el hecho; un
    asunto solo si hay mora con consecuencia). Los más recientes primero."""
    rows = (
        conn.execute(
            text(
                """
                SELECT c.id, c.status, c.kind, c.amount, c.currency, c.category, c.counterparty,
                       c.occurred_at, c.description,
                       (CURRENT_DATE - c.occurred_at::date) AS age_days
                FROM mod_finance_consolidated c
                WHERE c.user_id = :uid AND NOT c.deleted AND c.direction = 'egreso'
                  AND c.status IN ('fallida', 'pendiente')
                  AND NOT EXISTS (
                      SELECT 1 FROM mod_finance_consolidated d
                      WHERE d.user_id = c.user_id AND NOT d.deleted AND d.status = 'completada'
                        AND d.kind = 'pago' AND d.direction = 'egreso'
                        AND lower(d.counterparty) = lower(c.counterparty)
                        AND d.occurred_at >= c.occurred_at
                        AND d.occurred_at < c.occurred_at + make_interval(days => :days))
                ORDER BY c.occurred_at DESC, c.id DESC
                LIMIT :limit
                """
            ),
            {"uid": user_id, "days": resolve_days, "limit": limit},
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        d = dict(r)
        d["amount"] = float(d["amount"])
        d["age_days"] = int(d["age_days"] or 0)
        out.append(d)
    return out


def summarize_transactions(
    conn: Connection,
    user_id: int,
    *,
    by: str = "counterparty",
    q: str | None = None,
    category: str | None = None,
    direction: str | None = None,
    status: str | None = "completada",
    since: date | None = None,
    until: date | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Totales por `by` (contraparte | rubro | mes | moneda), siempre separados por moneda.
    `egresos`/`ingresos` son pagos; `reembolsos` es plata que volvió (kind reembolso);
    `gasto_neto` = egresos - reembolsos. Solo lo `completada` salvo `status='all'`."""
    if by not in SUMMARY_KEYS:
        raise ValueError(f"by inválido: {by!r}; válidos: {', '.join(SUMMARY_KEYS)}")
    where, params = _filters(
        q=q, category=category, direction=direction, status=status, since=since, until=until
    )
    params.update({"uid": user_id, "limit": limit})
    key = SUMMARY_KEYS[by]
    rows = (
        conn.execute(
            text(
                f"""
                SELECT {key} AS key, c.currency, count(*) AS count,
                       {_EGRESOS} AS egresos, {_INGRESOS} AS ingresos,
                       {_REEMBOLSOS} AS reembolsos,
                       MIN(c.occurred_at) AS first_at, MAX(c.occurred_at) AS last_at
                FROM mod_finance_consolidated c
                WHERE {" AND ".join(where)}
                GROUP BY 1, 2
                ORDER BY ({_EGRESOS} - {_REEMBOLSOS}) DESC, 1
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    out = []
    for r in rows:
        d = dict(r)
        for k in ("egresos", "ingresos", "reembolsos"):
            d[k] = float(d[k])
        d["gasto_neto"] = round(d["egresos"] - d["reembolsos"], 2)
        d["count"] = int(d["count"])
        out.append(d)
    return out
