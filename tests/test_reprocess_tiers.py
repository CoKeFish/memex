"""El reproceso por lote (botón "Ejecutar" del dashboard) respeta los tiers, igual que el daemon.

Regresión de H-1/H-2: la vía manual ignoraba la semántica de tiers (resumía/extraía sobre
`blacklist` y procesaba `batch` 1:1 en vez de ventanear). Estos tests fijan el contrato vía
`reprocess()` con un LLM falso (sin red):

- Lote (>1 target) → workers batch (`run_summarization`/`run_extraction`): salta blacklist,
  ventanea batch, individual 1:1.
- Un solo target (botón "Reprocesar" de /datos/:id) → vía per-mensaje (honra el click explícito,
  preserva la traza).

El LLM se inyecta parcheando el símbolo que importa `reprocess` y delegando al worker REAL con
`client=fake` (así se ejercita el ventaneo + force reales, sin tocar `DeepSeekClient`/la red).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules import resolve
from memex.modules.workset import load_module_workset
from memex.reprocess import reprocess
from memex.sources import kind_for_type
from memex.summarizer import worker as sw


class FakeLLM:
    """Satisface el Protocol LLMClient con una respuesta fija (sin red). Cuenta llamadas."""

    def __init__(self, content: str = "RESUMEN") -> None:
        self.calls = 0
        self._content = content

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(
            content=self._content,
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _patch_run_summarization(monkeypatch: pytest.MonkeyPatch, fake: FakeLLM) -> None:
    """Hace que la vía de LOTE delegue al worker real con `client=fake`."""
    real = sw.run_summarization

    async def _wrapped(user_id: int, **kw: Any) -> Any:
        kw["client"] = fake
        return await real(user_id, **kw)

    monkeypatch.setattr("memex.reprocess.run_summarization", _wrapped)


def _patch_summarize_inbox(monkeypatch: pytest.MonkeyPatch, fake: FakeLLM) -> None:
    """Hace que la vía de UN mensaje delegue a `summarize_inbox` real con `client=fake`."""
    real = sw.summarize_inbox

    async def _wrapped(user_id: int, inbox_id: int, **kw: Any) -> Any:
        kw["client"] = fake
        return await real(user_id, inbox_id, **kw)

    monkeypatch.setattr("memex.reprocess.summarize_inbox", _wrapped)


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


def _summarize(targets: list[int], *, force: bool = False) -> dict[str, Any]:
    out = asyncio.run(reprocess(1, stages=["summarize"], targets=targets, force=force))
    return dict(out["results"]["summarize"])


# ----- summarize por lote: respeta los tiers ------------------------------------- #


def test_bulk_skips_blacklist(monkeypatch: pytest.MonkeyPatch, seed_source: dict[str, Any]) -> None:
    """Lote: los mensajes blacklist NO se resumen (igual que el daemon)."""
    sid = seed_source["id"]
    ids = [
        _seed(sid, "b1", "blacklist", {"subject": "promo"}, minute=0),
        _seed(sid, "b2", "blacklist", {"subject": "promo2"}, minute=1),
    ]
    fake = FakeLLM()
    _patch_run_summarization(monkeypatch, fake)

    res = _summarize(ids)

    assert fake.calls == 0
    assert res["ok"] == 0
    assert _count("summaries") == 0


def test_bulk_windows_batch(monkeypatch: pytest.MonkeyPatch, seed_source: dict[str, Any]) -> None:
    """Lote: una secuencia batch contigua se resume en UNA ventana (no 1:1). Fix de H-1."""
    sid = seed_source["id"]
    ids = [_seed(sid, f"m{i}", "batch", {"text": f"t{i}"}, minute=i) for i in range(3)]
    fake = FakeLLM()
    _patch_run_summarization(monkeypatch, fake)

    res = _summarize(ids)

    assert fake.calls == 1  # una ventana, no tres
    assert res["ok"] == 1
    assert res["messages"] == 3
    assert _count("summaries") == 1
    assert _count("summary_inbox_links") == 3


def test_bulk_individual_one_each(
    monkeypatch: pytest.MonkeyPatch, seed_source: dict[str, Any]
) -> None:
    """Lote: tier individual → 1:1."""
    sid = seed_source["id"]
    ids = [
        _seed(sid, "i1", "individual", {"subject": "uno"}, minute=0),
        _seed(sid, "i2", "individual", {"subject": "dos"}, minute=1),
    ]
    fake = FakeLLM()
    _patch_run_summarization(monkeypatch, fake)

    res = _summarize(ids)

    assert fake.calls == 2
    assert res["ok"] == 2
    assert _count("summaries") == 2


def test_bulk_idempotent_without_force(
    monkeypatch: pytest.MonkeyPatch, seed_source: dict[str, Any]
) -> None:
    """Sin force, re-procesar lo ya resumido es no-op (no re-llama al LLM, no duplica)."""
    sid = seed_source["id"]
    ids = [_seed(sid, f"m{i}", "batch", {"text": f"t{i}"}, minute=i) for i in range(3)]
    fake = FakeLLM()
    _patch_run_summarization(monkeypatch, fake)

    first = _summarize(ids)
    second = _summarize(ids)

    assert fake.calls == 1  # la 2da no re-llama
    assert first["ok"] == 1
    assert second["ok"] == 0  # "sin cambios" (semántica idempotente correcta)
    assert _count("summaries") == 1


def test_bulk_force_rewindows_batch(
    monkeypatch: pytest.MonkeyPatch, seed_source: dict[str, Any]
) -> None:
    """Con force, la ventana batch se re-resume (borra + re-hace), sin duplicar."""
    sid = seed_source["id"]
    ids = [_seed(sid, f"m{i}", "batch", {"text": f"t{i}"}, minute=i) for i in range(3)]
    fake = FakeLLM()
    _patch_run_summarization(monkeypatch, fake)

    _summarize(ids)
    res = _summarize(ids, force=True)

    assert fake.calls == 2  # re-llamó en la corrida con force
    assert res["ok"] == 1
    assert _count("summaries") == 1
    assert _count("summary_inbox_links") == 3


def test_force_partial_window_does_not_orphan_comembers(
    monkeypatch: pytest.MonkeyPatch, seed_source: dict[str, Any]
) -> None:
    """Force sobre un SUBCONJUNTO de una ventana batch re-hace la ventana COMPLETA: el co-miembro
    no incluido en los targets NO queda huérfano (lo cubre `_force_clear_summaries`)."""
    sid = seed_source["id"]
    ids = [_seed(sid, f"m{i}", "batch", {"text": f"t{i}"}, minute=i) for i in range(3)]
    fake = FakeLLM()
    _patch_run_summarization(monkeypatch, fake)

    _summarize(ids)  # 1 ventana {m0,m1,m2}
    _summarize([ids[0], ids[1]], force=True)  # force solo 2 de los 3

    assert _count("summaries") == 1  # la ventana se reconstruyó entera
    assert _count("summary_inbox_links") == 3  # m2 NO quedó huérfano


def test_single_message_uses_per_message_path(
    monkeypatch: pytest.MonkeyPatch, seed_source: dict[str, Any]
) -> None:
    """Un solo target (botón 'Reprocesar' de /datos/:id) va por la vía per-mensaje y la resume."""
    iid = _seed(seed_source["id"], "i1", "individual", {"subject": "uno"})
    fake = FakeLLM()
    _patch_summarize_inbox(monkeypatch, fake)

    res = _summarize([iid])

    assert fake.calls == 1
    assert res["ok"] == 1
    assert _count("summaries") == 1


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
