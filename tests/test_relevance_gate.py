"""Gate de relevancia (LLM fake) + minería de reglas + etapa de reproceso.

`FakeGateLLM` implementa el Protocol y ramifica por system prompt (patrón `FakeExtractLLM`):
veredictos configurables por inbox_id para el gate; propuestas configurables para la minería.
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

from memex.core.deadletter import STAGE_RELEVANCE, list_review
from memex.db import connection
from memex.llm import ChatMessage, LLMQuotaError, LLMResult, LLMUsage, ResponseFormat
from memex.relevance import (
    VerdictItem,
    create_rule,
    dry_run_rule,
    insert_verdicts,
    run_relevance_gate,
    run_rule_mining,
    upsert_settings,
)
from memex.relevance.gate import GateStats
from memex.relevance.prompts import GATE_SYSTEM_PROMPT, RULES_SYSTEM_PROMPT
from memex.reprocess import STAGE_ORDER, reprocess


def _seed_source(name: str = "mail", source_type: str = "imap") -> int:
    with connection() as c:
        sid = c.execute(
            text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
            {"n": name, "t": source_type},
        ).scalar()
    assert sid is not None
    return int(sid)


def _seed_msg(
    source_id: int,
    ext: str,
    *,
    tier: str = "batch",
    sender: str = "promo@steam.com",
    subject: str = "Oferta",
    minute: int = 0,
    hour: int = 12,
) -> int:
    payload = {"from": {"email": sender}, "subject": subject, "body_text": f"cuerpo {ext}"}
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
                "occ": datetime(2026, 6, 1, hour, minute, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar()
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :iid, :tier)"),
            {"iid": iid, "tier": tier},
        )
    assert iid is not None
    return int(iid)


def _enable(mode: str = "per_window") -> None:
    with connection() as c:
        upsert_settings(c, 1, enabled=True, mode=mode)


def _verdict_rows() -> dict[int, tuple[str, str]]:
    with connection() as c:
        rows = c.execute(
            text("SELECT inbox_id, verdict, method FROM relevance_verdicts ORDER BY inbox_id")
        ).all()
    return {int(r[0]): (str(r[1]), str(r[2])) for r in rows}


class FakeGateLLM:
    """Ramifica por system prompt: gate → veredictos por id; minería → propuestas fijas."""

    def __init__(
        self,
        verdicts: dict[int, str] | None = None,
        *,
        default_verdict: str = "relevant",
        rules: list[dict[str, str]] | None = None,
        gate_content: str | None = None,
        finish_reason: str = "stop",
        quota: bool = False,
    ) -> None:
        self.calls = 0
        self.gate_calls = 0
        self.mining_calls = 0
        self.seen_ids: list[list[int]] = []
        self._verdicts = verdicts or {}
        self._default = default_verdict
        self._rules = rules or []
        self._gate_content = gate_content
        self._finish = finish_reason
        self._quota = quota

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
        if self._quota:
            raise LLMQuotaError(400, "insufficient credit balance")
        system = messages[0].content
        user = messages[-1].content
        if system == GATE_SYSTEM_PROMPT:
            self.gate_calls += 1
            batch = json.loads(user.split("Mensajes (JSON):\n", 1)[1])
            ids = [int(item["id"]) for item in batch]
            self.seen_ids.append(ids)
            if self._gate_content is not None:
                content = self._gate_content
            else:
                content = json.dumps(
                    {
                        "verdicts": [
                            {
                                "id": i,
                                "verdict": self._verdicts.get(i, self._default),
                                "reason": "fake",
                            }
                            for i in ids
                        ]
                    }
                )
        elif system == RULES_SYSTEM_PROMPT:
            self.mining_calls += 1
            content = json.dumps({"rules": self._rules})
        else:  # pragma: no cover - prompt inesperado
            raise AssertionError(f"system prompt inesperado: {system[:60]}")
        return LLMResult(
            content=content,
            model="claude-opus-4-8",
            usage=LLMUsage(100, 10, 110, cache_hit_tokens=0, cache_miss_tokens=100),
            cost_usd=Decimal("0.001"),
            latency_ms=5,
            finish_reason=self._finish,
        )


# ---------------------------------------------------------------- gate


def test_gate_disabled_is_noop() -> None:
    sid = _seed_source()
    _seed_msg(sid, "m1")
    llm = FakeGateLLM()
    stats = asyncio.run(run_relevance_gate(1, client=llm))
    assert llm.calls == 0
    assert stats.windows == 0
    assert _verdict_rows() == {}


def test_gate_per_window_one_call_per_window() -> None:
    _enable("per_window")
    sid = _seed_source()
    a = _seed_msg(sid, "m1", minute=0)
    b = _seed_msg(sid, "m2", minute=1)
    c = _seed_msg(sid, "m3", minute=2)
    llm = FakeGateLLM({a: "relevant", b: "not_relevant", c: "insufficient"})
    stats = asyncio.run(run_relevance_gate(1, client=llm))
    assert llm.gate_calls == 1
    assert llm.seen_ids == [[a, b, c]]
    assert stats.relevant == 1 and stats.not_relevant == 1 and stats.insufficient == 1
    rows = _verdict_rows()
    assert rows[a] == ("relevant", "llm")
    assert rows[b] == ("not_relevant", "llm")
    assert rows[c] == ("insufficient", "llm")
    with connection() as conn:
        mode = conn.execute(text("SELECT DISTINCT mode FROM relevance_verdicts")).scalar_one()
        assert mode == "per_window"
    # llm_calls registrada con purpose propio
    with connection() as conn:
        purpose = conn.execute(
            text("SELECT purpose, status FROM llm_calls ORDER BY id DESC LIMIT 1")
        ).first()
    assert purpose is not None and (purpose[0], purpose[1]) == ("relevance_gate", "ok")


def test_gate_per_message_one_call_each() -> None:
    _enable("per_message")
    sid = _seed_source()
    a = _seed_msg(sid, "m1", minute=0)
    b = _seed_msg(sid, "m2", minute=1)
    llm = FakeGateLLM({a: "relevant", b: "not_relevant"})
    stats = asyncio.run(run_relevance_gate(1, client=llm))
    assert llm.gate_calls == 2
    assert llm.seen_ids == [[a], [b]]
    assert stats.messages == 2
    assert _verdict_rows()[b] == ("not_relevant", "llm")


def test_gate_rules_prefilter_without_llm() -> None:
    _enable("per_window")
    sid = _seed_source()
    noise = _seed_msg(sid, "m1", sender="spam@promos.io", minute=0)
    real = _seed_msg(sid, "m2", sender="banco@bank.com", minute=1)
    with connection() as conn:
        rule = create_rule(
            conn,
            1,
            kind="sender_domain",
            pattern="promos.io",
            proposed_by="manual",
            report=dry_run_rule(conn, 1, "sender_domain", "promos.io"),
        )
        assert rule is not None and rule["status"] == "active"
    llm = FakeGateLLM(default_verdict="relevant")
    stats = asyncio.run(run_relevance_gate(1, client=llm))
    assert llm.seen_ids == [[real]]  # el matcheado por regla no llega al LLM
    assert stats.by_rule == 1
    rows = _verdict_rows()
    assert rows[noise] == ("not_relevant", "rule")
    assert rows[real] == ("relevant", "llm")
    with connection() as conn:
        rule_id = conn.execute(
            text("SELECT rule_id FROM relevance_verdicts WHERE inbox_id = :i"), {"i": noise}
        ).scalar()
    assert rule_id == rule["id"]


def test_gate_unparseable_is_retryable_with_deadletter() -> None:
    _enable("per_window")
    sid = _seed_source()
    iid = _seed_msg(sid, "m1")
    bad = FakeGateLLM(gate_content="no es json")
    stats = asyncio.run(run_relevance_gate(1, client=bad))
    assert stats.errors == 1
    assert _verdict_rows() == {}  # sin veredictos → reintentable
    with connection() as conn:
        wf = conn.execute(
            text("SELECT stage, attempts FROM work_item_failures WHERE inbox_id = :i"),
            {"i": iid},
        ).first()
    assert wf is not None and wf[0] == STAGE_RELEVANCE
    # status='error' en llm_calls
    with connection() as conn:
        status = conn.execute(
            text("SELECT status FROM llm_calls WHERE purpose = 'relevance_gate'")
        ).scalar_one()
    assert status == "error"
    # Reintento con LLM sano → persiste
    good = FakeGateLLM(default_verdict="relevant")
    asyncio.run(run_relevance_gate(1, client=good))
    assert _verdict_rows()[iid] == ("relevant", "llm")


def test_gate_missing_ids_fall_back_to_insufficient() -> None:
    _enable("per_window")
    sid = _seed_source()
    a = _seed_msg(sid, "m1", minute=0)
    b = _seed_msg(sid, "m2", minute=1)
    # El fake responde SOLO por `a`: b debe caer a insufficient (conservador)
    content = json.dumps({"verdicts": [{"id": a, "verdict": "relevant", "reason": "ok"}]})
    llm = FakeGateLLM(gate_content=content)
    asyncio.run(run_relevance_gate(1, client=llm))
    rows = _verdict_rows()
    assert rows[a] == ("relevant", "llm")
    assert rows[b][0] == "insufficient"


def test_gate_idempotent_second_run_no_llm() -> None:
    _enable("per_window")
    sid = _seed_source()
    _seed_msg(sid, "m1")
    llm = FakeGateLLM(default_verdict="relevant")
    asyncio.run(run_relevance_gate(1, client=llm))
    assert llm.gate_calls == 1
    again = FakeGateLLM(default_verdict="not_relevant")
    stats = asyncio.run(run_relevance_gate(1, client=again))
    assert again.calls == 0  # nada pendiente
    assert stats.windows == 0


def test_gate_force_rejudges_but_keeps_manual() -> None:
    _enable("per_window")
    sid = _seed_source()
    a = _seed_msg(sid, "m1", minute=0)
    b = _seed_msg(sid, "m2", minute=1)
    with connection() as conn:
        insert_verdicts(
            conn,
            1,
            [
                VerdictItem(a, "not_relevant", "llm"),
                VerdictItem(b, "relevant", "manual"),
            ],
        )
    llm = FakeGateLLM(default_verdict="relevant")
    asyncio.run(run_relevance_gate(1, inbox_ids=[a, b], force=True, client=llm))
    assert llm.seen_ids == [[a]]  # b es manual: no se re-juzga
    rows = _verdict_rows()
    assert rows[a] == ("relevant", "llm")
    assert rows[b] == ("relevant", "manual")


def test_gate_quota_error_aborts() -> None:
    _enable("per_window")
    sid = _seed_source()
    _seed_msg(sid, "m1")
    llm = FakeGateLLM(quota=True)
    with pytest.raises(LLMQuotaError):
        asyncio.run(run_relevance_gate(1, client=llm))


def test_gate_truncated_response_is_error() -> None:
    _enable("per_window")
    sid = _seed_source()
    iid = _seed_msg(sid, "m1")
    llm = FakeGateLLM(default_verdict="relevant", finish_reason="length")
    stats = asyncio.run(run_relevance_gate(1, client=llm))
    assert stats.errors == 1
    assert _verdict_rows() == {}
    assert any(r["inbox_id"] == iid for r in _deadletter_rows())


def _deadletter_rows() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(text("SELECT inbox_id, stage FROM work_item_failures")).mappings().all()
    return [dict(r) for r in rows]


def test_gate_deadletter_review_excludes_from_workset() -> None:
    """3 fallos → review → el workset del gate lo excluye (no se reintenta más)."""
    _enable("per_window")
    sid = _seed_source()
    _seed_msg(sid, "m1")
    bad = FakeGateLLM(gate_content="basura")
    for _ in range(3):
        asyncio.run(run_relevance_gate(1, client=bad))
    assert bad.gate_calls == 3
    assert [r["inbox_id"] for r in list_review(1, STAGE_RELEVANCE)]
    again = FakeGateLLM(default_verdict="relevant")
    stats = asyncio.run(run_relevance_gate(1, client=again))
    assert again.calls == 0
    assert stats.windows == 0


# ---------------------------------------------------------------- minería


def test_mining_activates_good_rejects_bad_skips_dup() -> None:
    _enable("per_window")
    sid = _seed_source()
    # Histórico: spam.io = puro ruido; bank.com tiene un correo RELEVANTE
    s1 = _seed_msg(sid, "m1", sender="a@spam.io", minute=0)
    s2 = _seed_msg(sid, "m2", sender="b@spam.io", minute=1)
    bank_noise = _seed_msg(sid, "m3", sender="promo@bank.com", minute=2)
    bank_real = _seed_msg(sid, "m4", sender="alertas@bank.com", minute=3)
    with connection() as conn:
        insert_verdicts(
            conn,
            1,
            [
                VerdictItem(s1, "not_relevant", "llm"),
                VerdictItem(s2, "not_relevant", "llm"),
                VerdictItem(bank_noise, "not_relevant", "llm"),
                VerdictItem(bank_real, "relevant", "llm"),
            ],
        )
    proposals = [
        {"kind": "sender_domain", "pattern": "spam.io", "rationale": "todo ruido"},
        {"kind": "sender_domain", "pattern": "bank.com", "rationale": "promos"},
        {"kind": "sender_domain", "pattern": "spam.io", "rationale": "duplicada"},
    ]
    with connection() as conn:
        upsert_settings(conn, 1, mining_min_messages=2)  # spam.io (2) llega; bank.com (1) no
    llm = FakeGateLLM(rules=proposals)
    stats = asyncio.run(run_rule_mining(1, client=llm))
    assert llm.mining_calls == 1
    assert stats.proposed == 3
    assert stats.activated == 1  # spam.io
    assert stats.rejected == 1  # bank.com atrapa al relevante
    assert stats.skipped == 1  # duplicada
    with connection() as conn:
        rows = conn.execute(
            text("SELECT pattern, status, proposed_by FROM relevance_gate_rules ORDER BY id")
        ).all()
    assert [(r[0], r[1], r[2]) for r in rows] == [
        ("spam.io", "active", "llm"),
        ("bank.com", "rejected", "llm"),
    ]
    # purpose propio en llm_calls
    with connection() as conn:
        assert (
            conn.execute(
                text("SELECT COUNT(*) FROM llm_calls WHERE purpose = 'relevance_rules'")
            ).scalar()
            == 1
        )


def test_mining_noop_when_disabled_or_empty() -> None:
    llm = FakeGateLLM()
    stats = asyncio.run(run_rule_mining(1, client=llm))
    assert llm.calls == 0 and stats.proposed == 0  # apagado
    _enable()
    stats = asyncio.run(run_rule_mining(1, client=llm))
    assert llm.calls == 0 and stats.senders == 0  # sin no-relevantes


def test_mining_accumulation_threshold_and_rule_method_excluded() -> None:
    """Un solo correo malo NO dispara nada: el umbral exige N+ no-relevantes acumulados por
    remitente, y los ya cubiertos por regla (`method='rule'`) no cuentan (clase resuelta)."""
    _enable()
    sid = _seed_source()
    a = _seed_msg(sid, "m1", sender="a@spam.io", minute=0)
    b = _seed_msg(sid, "m2", sender="b@spam.io", minute=1)
    with connection() as conn:
        insert_verdicts(
            conn,
            1,
            [
                VerdictItem(a, "not_relevant", "llm"),
                VerdictItem(b, "not_relevant", "rule"),  # ya cubierto: no alimenta la minería
            ],
        )
        upsert_settings(conn, 1, mining_min_messages=2)
    llm = FakeGateLLM(rules=[{"kind": "sender_domain", "pattern": "spam.io", "rationale": "x"}])
    stats = asyncio.run(run_rule_mining(1, client=llm))
    assert llm.calls == 0  # solo 1 'llm' acumulado < umbral 2 → ni se llama al LLM
    assert stats.senders == 0 and stats.proposed == 0
    # Override del umbral por corrida (CLI --min-count / experimentación)
    stats = asyncio.run(run_rule_mining(1, min_messages=1, client=llm))
    assert llm.mining_calls == 1
    assert stats.senders == 1 and stats.activated == 1


# ---------------------------------------------------------------- etapa de reproceso


def test_reprocess_stage_order_and_disabled_noop() -> None:
    assert STAGE_ORDER == ("media", "ocr", "classify", "relevance", "extract")
    sid = _seed_source()
    iid = _seed_msg(sid, "m1")
    out = asyncio.run(reprocess(1, stages=["relevance"], targets=[iid]))
    assert out["results"]["relevance"]["messages"] == 0  # gate apagado → no-op
    assert out["cost_usd"] == 0


def test_gate_stats_shape() -> None:
    s = GateStats()
    s.bump("relevant")
    s.bump("not_relevant")
    s.bump("insufficient")
    assert (s.relevant, s.not_relevant, s.insufficient) == (1, 1, 1)
