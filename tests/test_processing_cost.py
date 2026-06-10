"""Costo en corridas de procesamiento: total de reprocess() + serialización de stats con Decimal.

Sin LLM: `classify` es determinista y `ocr` con cero assets pendientes corta antes de construir
el cliente de visión — ambos slots deben reportar costo (0.0) igual, y el total de la corrida es
la suma de etapas.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.core.observability import CostBySource
from memex.db import connection
from memex.reprocess import reprocess
from memex.scheduler import runs


def _seed_inbox(source_id: int) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, 'cost-1', NOW(), CAST(:p AS JSONB))
                RETURNING id
                """
            ),
            {"sid": source_id, "p": json.dumps({"subject": "hola", "body_text": "x"})},
        ).scalar()
    assert isinstance(iid, int)
    return iid


def test_reprocess_reports_total_cost(seed_source: dict[str, Any]) -> None:
    iid = _seed_inbox(seed_source["id"])
    out = asyncio.run(reprocess(1, stages=["ocr", "classify"], targets=[iid]))

    assert out["cost_usd"] == 0.0  # etapas gratis: total presente e igual a la suma
    assert out["results"]["ocr"]["cost_usd"] == 0.0  # el slot ocr ahora reporta su costo
    assert out["results"]["classify"]["classified"] == 1


def test_finish_run_serializes_decimal_stats() -> None:
    """Las stats de jobs (CostBySource) cargan Decimal: finish_run no debe reventar al persistir."""

    @dataclass
    class FakeJobStats:
        ok: int = 1
        cost: CostBySource = field(default_factory=CostBySource)

    stats = FakeJobStats()
    stats.cost.record(None, prompt_tokens=10, completion_tokens=5, cost_usd=Decimal("0.001"))
    stats.cost.record(7, prompt_tokens=20, completion_tokens=9, cost_usd=Decimal("0.002"))

    rid = runs.start_run(1, "summarize")
    runs.finish_run(rid, status="ok", stats=stats)

    with connection() as c:
        row = c.execute(text("SELECT stats FROM worker_runs WHERE id = :id"), {"id": rid}).scalar()
    assert row is not None
    assert row["ok"] == 1
    assert row["cost"]["total"]["cost_usd"] == pytest.approx(0.003)
    assert row["cost"]["total"]["calls"] == 2
