"""Backfill one-time de `module_extractions.item_count` (señal de relevancia, migración 0048).

Cuando se agregó `item_count`, las filas históricas quedaron en 0 → la métrica de relevancia las ve
como "sin hecho" (inertes), sesgando la detección de candidatos contra remitentes viejos. Este
backfill deriva el conteo real POR MENSAJE de las tablas de dominio de cada módulo (vía su puerta
pública `read_for_inbox`), exactamente como lo escribe el orquestador hacia adelante → cero
divergencia. Determinista, sin LLM, idempotente. Lo corre `memex-quality backfill-counts`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Connection, text

from memex.db import connection
from memex.logging import get_logger
from memex.modules import known_modules, resolve
from memex.modules.contract import InterestModule

_log = get_logger("memex.quality.backfill")


@dataclass
class BackfillStats:
    scanned: int = 0  # filas-cursor con item_count=0 evaluadas
    updated: int = 0  # filas con item_count corregido (>0)


def _counts_for(
    module: InterestModule, conn: Connection, user_id: int, inbox_ids: list[int]
) -> dict[int, int]:
    """Filas públicas por inbox para `module`. Camino rápido si `read_for_inbox` expone
    `source_inbox_ids` (un query); si no, cae a per-inbox (N queries)."""
    rows = module.read_for_inbox(conn, user_id, inbox_ids)
    counts: dict[int, int] = {}
    if rows and "source_inbox_ids" in rows[0]:
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


def backfill_item_counts(user_id: int) -> BackfillStats:
    """Recalcula `item_count` de las filas en 0 desde las tablas de dominio. Idempotente."""
    stats = BackfillStats()
    with connection() as conn:
        for slug in known_modules():
            module = resolve(slug)()
            ids = [
                int(r[0])
                for r in conn.execute(
                    text(
                        "SELECT inbox_id FROM module_extractions "
                        "WHERE user_id = :uid AND module_slug = :s AND item_count = 0"
                    ),
                    {"uid": user_id, "s": slug},
                ).all()
            ]
            if not ids:
                continue
            stats.scanned += len(ids)
            for inbox_id, cnt in _counts_for(module, conn, user_id, ids).items():
                if cnt <= 0:
                    continue
                conn.execute(
                    text(
                        "UPDATE module_extractions SET item_count = :c "
                        "WHERE module_slug = :s AND inbox_id = :i"
                    ),
                    {"c": cnt, "s": slug, "i": inbox_id},
                )
                stats.updated += 1
    _log.info(
        "quality.backfill.item_count",
        user_id=user_id,
        scanned=stats.scanned,
        updated=stats.updated,
    )
    return stats
