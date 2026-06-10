"""Conteo de hechos ATRIBUIDOS por mensaje desde las tablas de dominio.

`module_extractions.item_count` = hechos públicos que el dominio ATRIBUYE a cada mensaje vía la
puerta `read_for_inbox` (overlap de `source_inbox_ids`): lo que el mensaje contiene/respalda HOY,
aunque el dedup haya unido sus hechos a filas pre-existentes. ÚNICA fuente del conteo — la
consumen el orquestador al escribir el cursor (los tres caminos: persist / empty_input /
ruteado-fuera) y `memex-quality backfill-counts` (reparación histórica) → cero divergencia por
construcción.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Connection

from memex.modules.contract import InterestModule


def attributed_counts(
    module: InterestModule, conn: Connection, user_id: int, inbox_ids: Sequence[int]
) -> dict[int, int]:
    """Filas públicas por inbox para `module`. Camino rápido si `read_for_inbox` expone
    `source_inbox_ids` (un query); si no, cae a per-inbox (N queries). Sin filas atribuidas al
    lote devuelve `{}` (→ item_count 0) sin pagar las N queries."""
    rows = module.read_for_inbox(conn, user_id, inbox_ids)
    if not rows:
        return {}
    counts: dict[int, int] = {}
    if "source_inbox_ids" in rows[0]:
        idset = set(inbox_ids)
        for row in rows:
            for raw in row.get("source_inbox_ids") or []:
                iid = int(raw)
                if iid in idset:
                    counts[iid] = counts.get(iid, 0) + 1
        return counts
    for iid in inbox_ids:
        n = len(module.read_for_inbox(conn, user_id, [iid]))
        if n:
            counts[iid] = n
    return counts
