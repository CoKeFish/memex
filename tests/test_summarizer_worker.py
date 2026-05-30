"""Worker del summarizer contra la DB (sembrada), con un LLM falso (sin red).

Cubre el camino feliz + los edge cases de manejo de fallos (content vacío/truncado,
error del LLM a mitad de corrida, input vacío, multi-source, unicidad anti-duplicación).
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
from sqlalchemy.exc import IntegrityError

from memex.db import connection
from memex.llm import ChatMessage, LLMError, LLMResult, LLMUsage, ResponseFormat
from memex.summarizer.worker import run_summarization


class FakeLLM:
    """Satisface el Protocol LLMClient. Configurable: content, finish_reason, fallo en N-ésima."""

    def __init__(
        self, content: str = "RESUMEN", finish_reason: str = "stop", fail_on_call: int | None = None
    ) -> None:
        self.calls = 0
        self._content = content
        self._finish = finish_reason
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
            raise LLMError(500, "boom en la llamada")
        return LLMResult(
            content=self._content,
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason=self._finish,
        )


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


def _new_source(name: str, source_type: str = "telegram") -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": name, "t": source_type},
        ).scalar()
    assert sid is not None
    return int(sid)


def _count(table: str) -> int:
    with connection() as c:
        return int(c.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0)


def _count_status(status: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("SELECT count(*) FROM llm_calls WHERE status = :s"), {"s": status}
            ).scalar()
            or 0
        )


def _first_summary_metadata() -> dict[str, Any]:
    with connection() as c:
        md = c.execute(text("SELECT metadata FROM summaries ORDER BY id LIMIT 1")).scalar()
    assert isinstance(md, dict)
    return md


# ----- camino feliz -------------------------------------------------------------- #


def test_batch_window_one_summary(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _seed(sid, "m1", "batch", {"text": "hola"}, minute=0)
    _seed(sid, "m2", "batch", {"text": "qué tal"}, minute=1)
    _seed(sid, "m3", "batch", {"text": "todo bien"}, minute=2)

    fake = FakeLLM()
    stats = asyncio.run(run_summarization(1, client=fake))

    assert fake.calls == 1
    assert stats.summaries == 1
    assert stats.messages == 3
    assert _count("summaries") == 1
    assert _count("summary_inbox_links") == 3


def test_individual_one_summary_each(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _seed(sid, "m1", "individual", {"subject": "uno"})
    _seed(sid, "m2", "individual", {"subject": "dos"})

    fake = FakeLLM()
    stats = asyncio.run(run_summarization(1, client=fake))

    assert fake.calls == 2
    assert stats.summaries == 2
    assert _count("summary_inbox_links") == 2


def test_blacklist_is_skipped(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "blacklist", {"subject": "promo"})

    fake = FakeLLM()
    stats = asyncio.run(run_summarization(1, client=fake))

    assert fake.calls == 0
    assert stats.summaries == 0
    assert _count("summaries") == 0


def test_idempotent(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "individual", {"subject": "uno"})

    fake = FakeLLM()
    asyncio.run(run_summarization(1, client=fake))
    second = asyncio.run(run_summarization(1, client=fake))

    assert fake.calls == 1
    assert second.summaries == 0
    assert _count("summaries") == 1


def test_tier_filter(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _seed(sid, "b1", "batch", {"text": "grupo"})
    _seed(sid, "i1", "individual", {"subject": "importante"})

    fake = FakeLLM()
    stats = asyncio.run(run_summarization(1, tier="individual", client=fake))

    assert stats.summaries == 1
    assert stats.by_tier.get("individual") == 1
    assert "batch" not in stats.by_tier


def test_multi_source_separate_summaries(seed_source: dict[str, Any]) -> None:
    sid_a = seed_source["id"]
    sid_b = _new_source("src-b")
    _seed(sid_a, "a1", "batch", {"text": "de A"})
    _seed(sid_b, "b1", "batch", {"text": "de B"})

    fake = FakeLLM()
    stats = asyncio.run(run_summarization(1, client=fake))

    assert fake.calls == 2  # fuentes distintas → ventanas distintas
    assert stats.summaries == 2


# ----- manejo de fallos / edge cases --------------------------------------------- #


def test_empty_content_not_persisted(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "batch", {"text": "hola"})

    fake = FakeLLM(content="")
    stats = asyncio.run(run_summarization(1, client=fake))

    assert fake.calls == 1  # llamó, pero
    assert stats.summaries == 0
    assert stats.skipped == 1
    assert _count("summaries") == 0
    assert _count("summary_inbox_links") == 0
    assert _count_status("error") == 1  # el costo se registró como error
    # reintentable: vuelve a aparecer en el work-set
    again = asyncio.run(run_summarization(1, client=FakeLLM(content="ok")))
    assert again.summaries == 1


def test_whitespace_content_rejected(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "batch", {"text": "hola"})

    stats = asyncio.run(run_summarization(1, client=FakeLLM(content="  \n  ")))

    assert stats.summaries == 0
    assert stats.skipped == 1
    assert _count("summaries") == 0


def test_truncated_response_is_flagged(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "individual", {"subject": "largo"})

    stats = asyncio.run(
        run_summarization(1, client=FakeLLM(content="resumen cortado", finish_reason="length"))
    )

    assert stats.summaries == 1  # truncado se persiste igual
    md = _first_summary_metadata()
    assert md["truncated"] is True
    assert md["finish_reason"] == "length"


def test_empty_input_payload_skipped_without_llm(seed_source: dict[str, Any]) -> None:
    _seed(seed_source["id"], "m1", "batch", {})  # renderiza a ""

    fake = FakeLLM()
    stats = asyncio.run(run_summarization(1, client=fake))

    assert fake.calls == 0  # ni siquiera llama al LLM
    assert stats.summaries == 0
    assert stats.skipped == 1


def test_llm_error_mid_run_is_idempotent(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _seed(sid, "i1", "individual", {"subject": "uno"})
    _seed(sid, "i2", "individual", {"subject": "dos"})

    fake = FakeLLM(fail_on_call=2)  # la 2da ventana explota
    stats = asyncio.run(run_summarization(1, client=fake))

    assert stats.summaries == 1
    assert stats.errors == 1
    assert _count("summaries") == 1
    assert _count_status("error") == 1
    # la ventana que falló sigue sin resumir → una corrida nueva la procesa, sin duplicar la 1ra
    second = asyncio.run(run_summarization(1, client=FakeLLM()))
    assert second.summaries == 1
    assert _count("summaries") == 2


def test_inbox_belongs_to_at_most_one_summary(seed_source: dict[str, Any]) -> None:
    """UNIQUE(inbox_id) de la 0007: un mensaje no puede ligarse a dos summaries."""
    iid = _seed(seed_source["id"], "m1", "individual", {"subject": "x"})
    asyncio.run(run_summarization(1, client=FakeLLM()))  # liga iid a un summary

    with connection() as c:
        other = c.execute(
            text(
                "INSERT INTO summaries (user_id, tier, content) "
                "VALUES (1, 'batch', 'otro') RETURNING id"
            )
        ).scalar()
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:s, :i)"),
            {"s": other, "i": iid},
        )
