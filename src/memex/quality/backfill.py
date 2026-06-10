"""Backfill de `module_extractions.item_count` (señal de relevancia, migración 0048).

Repara filas históricas con `item_count=0` que el dominio SÍ respalda (las que dejó la migración
0048 y las que un write-path viejo grabó en 0 al reprocesar): la métrica de relevancia las ve como
"sin hecho" (inertes), sesgando la detección de candidatos. Deriva el conteo real POR MENSAJE de
las tablas de dominio con la MISMA función que usa el orquestador al escribir el cursor
(`memex.modules.attribution.attributed_counts`) → cero divergencia por construcción. Determinista,
sin LLM, idempotente. Lo corre `memex-quality backfill-counts`.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger
from memex.modules import known_modules, resolve
from memex.modules.attribution import attributed_counts

_log = get_logger("memex.quality.backfill")


@dataclass
class BackfillStats:
    scanned: int = 0  # filas-cursor con item_count=0 evaluadas
    updated: int = 0  # filas con item_count corregido (>0)


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
            for inbox_id, cnt in attributed_counts(module, conn, user_id, ids).items():
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
