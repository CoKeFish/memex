"""Handler LLM de co-ocurrencia identidad↔identidad (overflow), con LLMClient FALSO (sin red).

Cubre: parseo ultra-defensivo (`_parse_pairs`); detección de correos over-cap
(`_find_overcap_emails`, cuenta SOLO identidades); el worker `run_cooccurrence_llm` end-to-end
(emite aristas `confirmed`/`producer='llm'`, idempotente, descarta ruido, best-effort por correo,
no llama sin overcap, `LLMQuotaError` propaga).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.llm.client import LLMQuotaError
from memex.modules.identidades.relations_llm import (
    _find_overcap_emails,
    _parse_pairs,
    run_cooccurrence_llm,
)
from memex.relations.edges import list_edges


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


class SeqLLM:
    """Por llamada devuelve un `content` (str) o lanza una excepción, según la lista `behaviors`."""

    def __init__(self, behaviors: list[str | Exception]) -> None:
        self.behaviors = behaviors
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
        b = self.behaviors[self.calls]
        self.calls += 1
        if isinstance(b, Exception):
            raise b
        return LLMResult(
            content=b,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


class QuotaLLM:
    """LLMClient falso que agota cuota en cada llamada."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise LLMQuotaError(402, "sin saldo")


# --- helpers de siembra ------------------------------------------------------------ #


def _identity(conn: Any, kind: str, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, :k, :n) RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
    )


def _mention(conn: Any, identity_id: int, inbox_ids: list[int], kind: str = "persona") -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades_mentions "
            "(user_id, source_inbox_ids, mentioned_name, resolved_kind, resolved_identity_id, "
            "evidence) VALUES (1, :ids, 'X', :k, :p, 'ev')"
        ),
        {"ids": inbox_ids, "k": kind, "p": identity_id},
    )


def _pair(e: Any) -> set[tuple[str, int]]:
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}


# --- _parse_pairs (puro) ----------------------------------------------------------- #


def test_parse_pairs_valid() -> None:
    assert _parse_pairs('{"pairs": [{"a_id": 1, "b_id": 2}]}', {1, 2}) == [(1, 2)]


def test_parse_pairs_canonicalizes_and_dedups() -> None:
    # b<a se canoniza a (a<b); el par repetido se descarta.
    raw = '{"pairs": [{"a_id": 3, "b_id": 1}, {"a_id": 1, "b_id": 3}]}'
    assert _parse_pairs(raw, {1, 3}) == [(1, 3)]


def test_parse_pairs_drops_unknown_self_and_bool() -> None:
    assert _parse_pairs("no soy json", {1, 2}) == []
    assert _parse_pairs('{"otra": 1}', {1, 2}) == []
    assert _parse_pairs('{"pairs": [{"a_id": 9, "b_id": 1}]}', {1, 2}) == []  # id fuera del set
    assert _parse_pairs('{"pairs": [{"a_id": 1, "b_id": 1}]}', {1, 2}) == []  # self-par
    assert _parse_pairs('{"pairs": [{"a_id": true, "b_id": 1}]}', {1, 2}) == []  # bool-como-int


# --- _find_overcap_emails (DB) ----------------------------------------------------- #


def test_find_overcap_emails(conn: Any) -> None:
    # inbox 7: 3 identidades (> cap 2) → over-cap; inbox 8: 2 identidades (= cap) → no.
    for n in ("A", "B", "C"):
        _mention(conn, _identity(conn, "persona", n), [7])
    for n in ("D", "E"):
        _mention(conn, _identity(conn, "persona", n), [8])
    assert _find_overcap_emails(conn, 1, 2, 100) == [7]


# --- run_cooccurrence_llm (worker, LLM falso) -------------------------------------- #


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_emits_confirmed_llm_edges() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}}}]}}')
    stats = await run_cooccurrence_llm(1, cap=1, client=fake)
    assert (stats.emails, stats.edges, stats.errors) == (1, 1, 0)
    assert stats.cost.calls == 1
    with connection() as c:
        edges = list_edges(c, 1)
        purpose = c.execute(text("SELECT purpose FROM llm_calls WHERE user_id = 1")).scalar_one()
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "llm"
    assert e.status == "confirmed"
    assert e.relation_type == "co-ocurrencia"
    assert e.evidence == "inbox:7"
    assert _pair(e) == {("identidades:person", p), ("identidades:org", o)}
    assert purpose == "identidades_cooccurrence"


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_idempotent() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}}}]}}')
    await run_cooccurrence_llm(1, cap=1, client=fake)
    await run_cooccurrence_llm(1, cap=1, client=fake)
    with connection() as c:
        assert len(list_edges(c, 1)) == 1  # ON CONFLICT: una sola fila


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_drops_noise_identity() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    # El LLM devuelve un id que NO está en el correo → el parseo defensivo lo descarta.
    fake = FakeLLM('{"pairs": [{"a_id": 999999, "b_id": 1}]}')
    stats = await run_cooccurrence_llm(1, cap=1, client=fake)
    assert stats.edges == 0 and fake.calls == 1
    with connection() as c:
        assert list_edges(c, 1) == []


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_no_overcap_email_no_call() -> None:
    # 2 identidades con el cap por default (8) → ningún correo supera el umbral → ni llama al LLM.
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    fake = FakeLLM('{"pairs": []}')
    stats = await run_cooccurrence_llm(1, client=fake)
    assert stats.emails == 0 and fake.calls == 0
    with connection() as c:
        assert list_edges(c, 1) == []


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_best_effort_one_bad_email() -> None:
    # inbox 7 OK, inbox 8 el LLM explota: el bueno emite, el malo es error, la corrida sigue.
    with connection() as c:
        a = _identity(c, "persona", "A")
        b = _identity(c, "organizacion", "B")
        _mention(c, a, [7])
        _mention(c, b, [7], kind="organizacion")
        d = _identity(c, "persona", "D")
        e2 = _identity(c, "persona", "E")
        _mention(c, d, [8])
        _mention(c, e2, [8])
    seq = SeqLLM([f'{{"pairs": [{{"a_id": {a}, "b_id": {b}}}]}}', RuntimeError("boom")])
    stats = await run_cooccurrence_llm(1, cap=1, client=seq)
    assert (stats.emails, stats.edges, stats.errors) == (2, 1, 1)
    with connection() as c:
        edges = list_edges(c, 1)
    assert len(edges) == 1
    assert _pair(edges[0]) == {("identidades:person", a), ("identidades:org", b)}


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_quota_propagates() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    with pytest.raises(LLMQuotaError):
        await run_cooccurrence_llm(1, cap=1, client=QuotaLLM())
