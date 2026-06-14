"""El reproceso por lote de EXTRACT (botón "Ejecutar" del dashboard) respeta los tiers.

Regresión de H-1/H-2: la vía manual ignoraba la semántica de tiers (procesaba `blacklist` y no
ventaneaba `batch`). El resumen dejó de ser una etapa de reproceso (lo produce la fase de
co-ocurrencia, ver `tests/relations/test_summary.py`); acá queda el contrato de extract vía
`reprocess()`: el work-set acotado por `inbox_ids` mantiene el filtro de tier (blacklist fuera).
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules import resolve
from memex.modules.workset import load_module_workset
from memex.reprocess import reprocess
from memex.sources import kind_for_type


def _seed(source_id: int, ext: str, tier: str, payload: dict[str, Any], minute: int = 0) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": ext,
                "occ": datetime(2026, 5, 28, 12, minute, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar()
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :iid, :tier)"),
            {"iid": iid, "tier": tier},
        )
    assert iid is not None
    return int(iid)


def _enable(slug: str = "finance") -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, enabled) "
                "VALUES (1, :slug, TRUE) "
                "ON CONFLICT (user_id, module_slug) DO UPDATE SET enabled = TRUE"
            ),
            {"slug": slug},
        )


def _count(table: str) -> int:
    with connection() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)


# ----- extract por lote: salta blacklist (igual que el daemon) -------------------- #


def test_extract_workset_excludes_blacklist_and_filters_ids(seed_source: dict[str, Any]) -> None:
    """El work-set de extracción acotado por `inbox_ids` mantiene el filtro de tier: el blacklist
    se excluye y el individual entra (no vacuo: prueba que la fuente SÍ la consume finance)."""
    finance = resolve("finance")()
    assert kind_for_type(str(seed_source["type"])) in finance.consumes_kinds  # precondición
    sid = seed_source["id"]
    b = _seed(sid, "b1", "blacklist", {"subject": "promo", "body_text": "x"}, minute=0)
    i = _seed(sid, "i1", "individual", {"subject": "factura", "body_text": "total 100"}, minute=1)

    with connection() as c:
        rows = load_module_workset(
            c, 1, source_id=None, modules=[finance], limit=100, inbox_ids=[b, i]
        )
    got = {r.inbox_id for r in rows}

    assert i in got  # individual entra
    assert b not in got  # blacklist excluido


def test_reprocess_extract_skips_blacklist(seed_source: dict[str, Any]) -> None:
    """Vía e2e: reproceso de extract por lote sobre solo-blacklist → 0 items, 0 filas (sin LLM:
    el work-set queda vacío y `run_extraction` retorna antes de tocar el cliente)."""
    _enable("finance")
    sid = seed_source["id"]
    ids = [
        _seed(sid, "b1", "blacklist", {"subject": "a", "body_text": "x"}, minute=0),
        _seed(sid, "b2", "blacklist", {"subject": "b", "body_text": "y"}, minute=1),
    ]

    out = asyncio.run(reprocess(1, stages=["extract"], targets=ids))

    assert out["results"]["extract"]["items"] == 0
    assert _count("module_extractions") == 0
