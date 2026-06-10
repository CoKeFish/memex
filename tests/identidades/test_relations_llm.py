"""Handler LLM de co-ocurrencia identidad↔identidad (overflow), con LLMClient FALSO (sin red).

Cubre: parseo ultra-defensivo (`_parse_pairs`); detección de correos over-cap
(`_find_overcap_emails`, cuenta SOLO identidades); el worker `run_cooccurrence_llm` end-to-end
(emite aristas `confirmed`/`producer='llm'`, idempotente, descarta ruido, best-effort por correo,
no llama sin overcap, `LLMQuotaError` propaga); y el GROUNDER (un par solo se confirma si su
`quote` es cita verificable de la evidencia que el LLM vio — ausente/corta/inventada descarta).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.core.trace import create_root
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


#: Evidencia por default ≥ `_MIN_QUOTE_NORM_LEN`: los tests felices la citan textual.
_EV = "Juan trabaja con Acme"


def _mention(
    conn: Any,
    identity_id: int,
    inbox_ids: list[int],
    kind: str = "persona",
    evidence: str = _EV,
) -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades_mentions "
            "(user_id, source_inbox_ids, mentioned_name, resolved_kind, resolved_identity_id, "
            "evidence) VALUES (1, :ids, 'X', :k, :p, :ev)"
        ),
        {"ids": inbox_ids, "k": kind, "p": identity_id, "ev": evidence},
    )


def _pair(e: Any) -> set[tuple[str, int]]:
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}


def _seed_inbox(conn: Any, ext: str) -> int:
    sid = conn.execute(
        text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id"),
        {"n": ext},
    ).scalar_one()
    iid = conn.execute(
        text(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :sid, :ext, :occ, CAST('{}' AS JSONB)) RETURNING id"
        ),
        {"sid": sid, "ext": ext, "occ": datetime(2026, 6, 3, 12, 0, tzinfo=UTC)},
    ).scalar_one()
    return int(iid)


# --- _parse_pairs (puro) ----------------------------------------------------------- #


def test_parse_pairs_valid() -> None:
    raw = '{"pairs": [{"a_id": 1, "b_id": 2, "quote": "trabajan juntos"}]}'
    assert _parse_pairs(raw, {1, 2}) == [(1, 2, "trabajan juntos")]


def test_parse_pairs_canonicalizes_and_dedups() -> None:
    # b<a se canoniza a (a<b); el par repetido se descarta (la primera cita gana).
    raw = (
        '{"pairs": [{"a_id": 3, "b_id": 1, "quote": "uno"},'
        ' {"a_id": 1, "b_id": 3, "quote": "dos"}]}'
    )
    assert _parse_pairs(raw, {1, 3}) == [(1, 3, "uno")]


def test_parse_pairs_drops_unknown_self_and_bool() -> None:
    assert _parse_pairs("no soy json", {1, 2}) == []
    assert _parse_pairs('{"otra": 1}', {1, 2}) == []
    assert _parse_pairs('{"pairs": [{"a_id": 9, "b_id": 1}]}', {1, 2}) == []  # id fuera del set
    assert _parse_pairs('{"pairs": [{"a_id": 1, "b_id": 1}]}', {1, 2}) == []  # self-par
    assert _parse_pairs('{"pairs": [{"a_id": true, "b_id": 1}]}', {1, 2}) == []  # bool-como-int


def test_parse_pairs_quote_ausente_o_no_string_es_vacio() -> None:
    # Sin `quote` (o con basura) el par se parsea con cita vacía: el grounder lo descarta después.
    assert _parse_pairs('{"pairs": [{"a_id": 1, "b_id": 2}]}', {1, 2}) == [(1, 2, "")]
    assert _parse_pairs('{"pairs": [{"a_id": 1, "b_id": 2, "quote": 5}]}', {1, 2}) == [(1, 2, "")]


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
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}, "quote": "trabaja con Acme"}}]}}')
    stats = await run_cooccurrence_llm(1, cap=1, client=fake)
    assert (stats.emails, stats.edges, stats.errors, stats.ungrounded) == (1, 1, 0, 0)
    assert stats.cost.calls == 1
    with connection() as c:
        edges = list_edges(c, 1)
        purpose = c.execute(text("SELECT purpose FROM llm_calls WHERE user_id = 1")).scalar_one()
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "llm"
    assert e.status == "confirmed"
    assert e.relation_type == "co-ocurrencia"
    assert e.evidence == "inbox:7 | trabaja con Acme"
    assert _pair(e) == {("identidades:person", p), ("identidades:org", o)}
    assert purpose == "identidades_cooccurrence"


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_idempotent() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}, "quote": "trabaja con Acme"}}]}}')
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
    seq = SeqLLM(
        [
            f'{{"pairs": [{{"a_id": {a}, "b_id": {b}, "quote": "trabaja con Acme"}}]}}',
            RuntimeError("boom"),
        ]
    )
    stats = await run_cooccurrence_llm(1, cap=1, client=seq)
    assert (stats.emails, stats.edges, stats.errors) == (2, 1, 1)
    with connection() as c:
        edges = list_edges(c, 1)
    assert len(edges) == 1
    assert _pair(edges[0]) == {("identidades:person", a), ("identidades:org", b)}


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_attaches_cost_to_root() -> None:
    # La co-ocurrencia es per-mensaje: si el correo tiene root de traza, su costo se cuelga ahí
    # (hoja `llm` "co-ocurrencia" bajo el root) para que el costo del mensaje sea fiel.
    with connection() as c:
        iid = _seed_inbox(c, "cooc-trace")
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [iid])
        _mention(c, o, [iid], kind="organizacion")
        root = create_root(c, user_id=1, inbox_id=iid, label="msg")
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}, "quote": "trabaja con Acme"}}]}}')
    await run_cooccurrence_llm(1, cap=1, client=fake)

    with connection() as c:
        rows = (
            c.execute(
                text(
                    "SELECT parent_id, label, llm_call_id FROM trace_nodes "
                    "WHERE inbox_id = :i AND kind = 'llm'"
                ),
                {"i": iid},
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]["parent_id"] == root  # cuelga del root del mensaje
    assert rows[0]["label"] == "co-ocurrencia"
    assert rows[0]["llm_call_id"] is not None


# --- grounder (la cita debe ser verificable) ---------------------------------------- #


@pytest.mark.asyncio
async def test_grounder_quote_inventado_descarta_el_par() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    fake = FakeLLM(
        f'{{"pairs": [{{"a_id": {p}, "b_id": {o}, "quote": "colaboran en el proyecto X"}}]}}'
    )
    stats = await run_cooccurrence_llm(1, cap=1, client=fake)
    assert (stats.edges, stats.ungrounded) == (0, 1)
    with connection() as c:
        assert list_edges(c, 1) == []


@pytest.mark.asyncio
async def test_grounder_quote_ausente_descarta_el_par() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}}}]}}')  # sin quote
    stats = await run_cooccurrence_llm(1, cap=1, client=fake)
    assert (stats.edges, stats.ungrounded) == (0, 1)
    with connection() as c:
        assert list_edges(c, 1) == []


@pytest.mark.asyncio
async def test_grounder_quote_corto_descarta_aunque_este_contenido() -> None:
    # "Juan" SÍ está en la evidencia pero su largo normalizado (4) < _MIN_QUOTE_NORM_LEN.
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}, "quote": "Juan"}}]}}')
    stats = await run_cooccurrence_llm(1, cap=1, client=fake)
    assert (stats.edges, stats.ungrounded) == (0, 1)


@pytest.mark.asyncio
async def test_grounder_normaliza_case_y_whitespace() -> None:
    # La cita matchea aunque difiera en mayúsculas y colapso de whitespace (incl. saltos).
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7], evidence="Juan   trabaja\ncon ACME")
        _mention(c, o, [7], kind="organizacion", evidence="otra cosa irrelevante")
    fake = FakeLLM(f'{{"pairs": [{{"a_id": {p}, "b_id": {o}, "quote": "juan trabaja con acme"}}]}}')
    stats = await run_cooccurrence_llm(1, cap=1, client=fake)
    assert (stats.edges, stats.ungrounded) == (1, 0)
    with connection() as c:
        edges = list_edges(c, 1)
    assert edges[0].evidence == "inbox:7 | juan trabaja con acme"


@pytest.mark.asyncio
async def test_run_cooccurrence_llm_quota_propagates() -> None:
    with connection() as c:
        p = _identity(c, "persona", "Juan")
        o = _identity(c, "organizacion", "Acme")
        _mention(c, p, [7])
        _mention(c, o, [7], kind="organizacion")
    with pytest.raises(LLMQuotaError):
        await run_cooccurrence_llm(1, cap=1, client=QuotaLLM())
