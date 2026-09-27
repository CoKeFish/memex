"""`newest_first` en la corrida «de una»: con tope, ¿«los N» son los últimos que llegaron o los más
viejos pendientes? Por default es orden de ingesta (id); con `newest_first` es occurred_at DESC.

Motivación: una corrida «desde agosto, tope 10» tomó Outlook del 2-ago (ingerido antes) en vez
de Gmail del 1-sep, que era lo que el dueño quería revisar al volver tras semanas.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memex.core.inbox import insert_record
from memex.core.source import SourceRecord
from memex.db import connection


def _seed(source_id: int, ext: str, when: datetime) -> None:
    with connection() as c:
        insert_record(
            c,
            user_id=1,
            source_id=source_id,
            record=SourceRecord(external_id=ext, occurred_at=when, payload={}, dedupe_keys=[]),
        )


def test_dry_run_newest_first_orders_by_occurred_at_desc(
    client: Any, seed_source: dict[str, Any]
) -> None:
    sid = seed_source["id"]
    # Ingesta en orden: viejo (id bajo) → nuevo → y un backfill de historia VIEJA con id ALTO.
    _seed(sid, "a", datetime(2026, 8, 2, 10, 0, tzinfo=UTC))
    _seed(sid, "b", datetime(2026, 9, 1, 10, 0, tzinfo=UTC))
    _seed(sid, "c", datetime(2026, 8, 31, 10, 0, tzinfo=UTC))
    _seed(sid, "old", datetime(2026, 7, 1, 10, 0, tzinfo=UTC))  # id más alto, fecha más vieja
    by_ext = {r["external_id"]: r["id"] for r in client.get("/inbox?limit=50").json()["items"]}

    body = {"stages": ["classify"], "limit": 2}
    default = client.post("/processing/dry-run", json=body).json()
    assert default["sample_ids"] == [by_ext["a"], by_ext["b"]]  # orden de ingesta

    newest = client.post("/processing/dry-run", json={**body, "newest_first": True}).json()
    assert newest["sample_ids"] == [by_ext["b"], by_ext["c"]]  # los 2 más nuevos por fecha
    assert newest["count"] == 2
