"""Orquestador de extracción contra la DB (sembrada), con un LLM falso (sin red).

Cubre: camino feliz, short-circuit del ruteo (1 módulo → sin LLM de ruteo), idempotencia,
descarte por atribución, pre-filtro consumes_kinds, módulo deshabilitado y best-effort.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMError, LLMResult, LLMUsage, ResponseFormat
from memex.modules.orchestrator import run_extraction


class FakeExtractLLM:
    """Satisface LLMClient. Lee los `id` del prompt y emite un gasto por mensaje (atribuible).

    - `bogus_id`: en vez de los ids reales, cita uno fuera del lote (alucinación).
    - `empty`: devuelve content vacío.
    - `fail_on_call`: lanza LLMError en la N-ésima llamada.
    """

    def __init__(
        self,
        *,
        bogus_id: int | None = None,
        empty: bool = False,
        fail_on_call: int | None = None,
    ) -> None:
        self.calls = 0
        self._bogus = bogus_id
        self._empty = empty
        self._fail_on = fail_on_call

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
        if self._fail_on is not None and self.calls == self._fail_on:
            raise LLMError(500, "boom")

        content = "" if self._empty else self._build(messages[-1].content)
        return LLMResult(
            content=content,
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )

    def _build(self, user_content: str) -> str:
        arr = json.loads(user_content[user_content.index("[") :])
        items = []
        for msg in arr:
            sid = self._bogus if self._bogus is not None else msg["id"]
            items.append(
                {
                    "source_inbox_ids": [sid],
                    "amount": "100.00",
                    "currency": "ARS",
                    "merchant": "Test",
                    "occurred_on": None,
                    "description": "gasto de prueba",
                    "evidence": msg["text"],
                }
            )
        return json.dumps({"items": items})


# ----- helpers ------------------------------------------------------------------- #


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


def _new_source(name: str, source_type: str) -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": name, "t": source_type},
        ).scalar()
    assert sid is not None
    return int(sid)


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


def _count_purpose(purpose: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("SELECT count(*) FROM llm_calls WHERE purpose = :p"), {"p": purpose}
            ).scalar()
            or 0
        )


# ----- camino feliz + short-circuit ---------------------------------------------- #


def test_extracts_expenses_from_batch_window(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]  # type 'imap' → EMAIL
    _enable()
    _seed(sid, "m1", "batch", {"subject": "luz", "body_text": "pagué $4500"}, minute=0)
    _seed(sid, "m2", "batch", {"subject": "agua", "body_text": "pagué $1200"}, minute=1)

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 1  # una ventana batch → una llamada de extracción
    assert stats.items == 2  # un gasto por mensaje
    assert _count("mod_finance_expenses") == 2
    assert _count("module_extractions") == 2


def test_short_circuit_no_routing_llm(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, client=FakeExtractLLM()))

    assert _count_purpose("module_route") == 0  # un solo módulo → no se gasta LLM en rutear
    assert _count_purpose("extract_finance") == 1


def test_individual_one_call_each(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _enable()
    _seed(sid, "i1", "individual", {"body_text": "pagué $4500"})
    _seed(sid, "i2", "individual", {"body_text": "pagué $1200"})

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 2
    assert stats.items == 2


# ----- idempotencia -------------------------------------------------------------- #


def test_idempotent(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})

    asyncio.run(run_extraction(1, client=FakeExtractLLM()))
    second = asyncio.run(run_extraction(1, client=FakeExtractLLM()))

    assert second.items == 0
    assert _count("mod_finance_expenses") == 1


# ----- atribución alucinada ------------------------------------------------------ #


def test_attribution_miss_discarded(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "individual", {"body_text": "pagué $4500"})

    stats = asyncio.run(run_extraction(1, client=FakeExtractLLM(bogus_id=999999)))

    assert stats.items == 0
    assert stats.discarded == 1
    assert _count("mod_finance_expenses") == 0
    # el mensaje igual quedó marcado (considerado) → no se reprocesa en loop
    assert _count("module_extractions") == 1


# ----- pre-filtro consumes_kinds ------------------------------------------------- #


def test_social_source_excluded(seed_source: dict[str, Any]) -> None:
    """finance no consume SOCIAL → un post de instagram no entra al work-set."""
    ig = _new_source("ig", "instagram")
    _enable()
    _seed(ig, "p1", "batch", {"text": "mirá esta promo $999"})

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 0
    assert stats.items == 0
    assert _count("mod_finance_expenses") == 0


# ----- módulo deshabilitado ------------------------------------------------------ #


def test_disabled_module_no_work(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "batch", {"body_text": "pagué $4500"})  # finance NO habilitado

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 0
    assert stats.items == 0


def test_blacklist_not_extracted(seed_source: dict[str, Any]) -> None:
    _enable()
    _seed(seed_source["id"], "m1", "blacklist", {"body_text": "promo"})

    fake = FakeExtractLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 0
    assert stats.items == 0


# ----- best-effort --------------------------------------------------------------- #


def test_error_mid_run_is_best_effort(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _enable()
    _seed(sid, "i1", "individual", {"body_text": "pagué $4500"})
    _seed(sid, "i2", "individual", {"body_text": "pagué $1200"})

    fake = FakeExtractLLM(fail_on_call=2)  # la 2da ventana explota
    stats = asyncio.run(run_extraction(1, client=fake))

    assert stats.items == 1
    assert stats.errors == 1
    assert _count("mod_finance_expenses") == 1
    # la ventana que falló sigue pendiente → otra corrida la procesa, sin duplicar la 1ra
    second = asyncio.run(run_extraction(1, client=FakeExtractLLM()))
    assert second.items == 1
    assert _count("mod_finance_expenses") == 2
