"""Desempate LLM (FASE 2) de identidades con un LLMClient FALSO (sin red).

Cubre: confirma (`same:true`) → `merge_identities` (la absorbida desaparece, sobrevive la otra con
alias); rechaza (`same:false`) → coexisten + candidato `rejected`; sesgo a coexistir ante respuesta
no parseable; idempotencia (un confirmado borra el candidato por cascade → no re-llama)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.identidades.dedup_llm import _parse_decision, run_merge_phase2


class FakeLLM:
    """Devuelve siempre el mismo `content`; cuenta llamadas. Cumple el Protocol LLMClient."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

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
            content=self.content,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _mk_identity(name: str, kind: str = "persona") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name) "
                    "VALUES (1, :k, :n) RETURNING id"
                ),
                {"k": kind, "n": name},
            ).scalar_one()
        )


def _seed_candidate(a_id: int, b_id: int) -> None:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades_merge_candidates "
                "(user_id, identity_a_id, identity_b_id, reason, score) "
                "VALUES (1, :a, :b, 'trgm_name', 0.7)"
            ),
            {"a": lo, "b": hi},
        )


def _exists(identity_id: int) -> bool:
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :i"), {"i": identity_id}
        ).scalar_one()
    return int(n) == 1


def _aliases(identity_id: int) -> list[str]:
    with connection() as c:
        return list(
            c.execute(
                text("SELECT aliases FROM mod_identidades WHERE id = :i"), {"i": identity_id}
            ).scalar_one()
        )


def _candidate_status(a_id: int, b_id: int) -> str | None:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    with connection() as c:
        row = c.execute(
            text(
                "SELECT status FROM mod_identidades_merge_candidates "
                "WHERE identity_a_id = :a AND identity_b_id = :b"
            ),
            {"a": lo, "b": hi},
        ).first()
    return str(row[0]) if row is not None else None


# ----- _parse_decision (puro) ---------------------------------------------------- #


def test_parse_same_true() -> None:
    d = _parse_decision('{"same": true, "confidence": 0.9, "rationale": "misma"}')
    assert d.same is True and d.confidence == 0.9


def test_parse_garbage_biases_to_coexist() -> None:
    d = _parse_decision("no soy json")
    assert d.same is False and d.rationale == "parse_fallback"


# ----- worker (DB + LLM falso) --------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirms_merges_and_keeps_alias() -> None:
    a = _mk_identity("Ada Lovelace")
    b = _mk_identity("Ada L.")
    surv, absb = min(a, b), max(a, b)
    _seed_candidate(a, b)

    fake = FakeLLM('{"same": true, "confidence": 0.95, "rationale": "misma persona"}')
    stats = await run_merge_phase2(1, client=fake)

    assert (stats.pairs, stats.merged, stats.rejected) == (1, 1, 0)
    assert not _exists(absb)  # la absorbida se fundió
    assert _exists(surv)
    # la superviviente ganó el nombre de la absorbida como alias
    absorbed_name = "Ada L." if absb == b else "Ada Lovelace"
    assert absorbed_name in _aliases(surv)


@pytest.mark.asyncio
async def test_rejects_keeps_both() -> None:
    a = _mk_identity("Ana Pérez")
    b = _mk_identity("Ana Gómez")
    _seed_candidate(a, b)

    fake = FakeLLM('{"same": false, "confidence": 0.8, "rationale": "homónimas"}')
    stats = await run_merge_phase2(1, client=fake)

    assert (stats.merged, stats.rejected) == (0, 1)
    assert _exists(a) and _exists(b)
    assert _candidate_status(a, b) == "rejected"


@pytest.mark.asyncio
async def test_unparseable_response_rejects() -> None:
    a = _mk_identity("X")
    b = _mk_identity("Y")
    _seed_candidate(a, b)

    stats = await run_merge_phase2(1, client=FakeLLM("no es json"))

    assert stats.rejected == 1
    assert _exists(a) and _exists(b)
    assert _candidate_status(a, b) == "rejected"


@pytest.mark.asyncio
async def test_idempotent_confirmed_candidate_gone() -> None:
    a = _mk_identity("Ada Lovelace")
    b = _mk_identity("Ada L.")
    _seed_candidate(a, b)

    fake = FakeLLM('{"same": true, "confidence": 0.9, "rationale": "x"}')
    await run_merge_phase2(1, client=fake)
    stats2 = await run_merge_phase2(1, client=fake)  # el candidato ya no existe (cascade)

    assert stats2.pairs == 0
    assert fake.calls == 1  # no re-llamó al LLM
