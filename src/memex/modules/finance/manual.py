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

from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.geo.places import get_place
from memex.logging import get_logger

_log = get_logger("memex.modules.finance.manual")


def show_transaction(conn: Connection, user_id: int, consolidated_id: int) -> dict[str, Any] | None:
    """El detalle de un pago consolidado con su lugar resuelto (LEFT JOIN al catálogo), o None si
    no existe (o está tombstoneado por un merge)."""
    row = (
        conn.execute(
            text(
                """
                SELECT c.id, c.direction, c.amount, c.currency, c.category, c.counterparty,
                       c.counterparty_identity_id, c.place, c.occurred_at,
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
