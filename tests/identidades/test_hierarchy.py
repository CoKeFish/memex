"""Organizador de PERTENENCIA («sub») de identidades con un LLMClient FALSO (sin red).

Cubre: parseo ultra-defensivo (`_parse_links`); aplicación de links (`_apply_links`) — por id, por
nombre nuevo (crea org `source='extraction'`, idempotente por `org_core`), reuso de padre existente,
salto de self-parent y de ciclos; el helper `would_create_cycle`; el rename `cleaned_name` OFF/ON; y
el worker `run_organize` end-to-end (aplica, cuenta, `LLMQuotaError` propaga)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.llm.client import LLMQuotaError
from memex.modules.identidades.hierarchy import (
    HierarchyLink,
    _apply_links,
    _parse_links,
    _resolve_or_create_parent,
    run_organize,
    would_create_cycle,
)


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


def _mk_org(conn: Any, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'organizacion',:n) RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def _parent_of(conn: Any, identity_id: int) -> int | None:
    row = conn.execute(
        text("SELECT parent_identity_id FROM mod_identidades WHERE id = :i"), {"i": identity_id}
    ).scalar()
    return int(row) if row is not None else None


# ----- _parse_links (puro) ------------------------------------------------------- #


def test_parse_links_valid() -> None:
    links = _parse_links('{"links": [{"child_id": 1, "parent_id": 2}]}', {1, 2})
    assert links == [HierarchyLink(child_id=1, parent_id=2, parent_name=None, cleaned_name=None)]


def test_parse_links_parent_name_and_cleaned() -> None:
    links = _parse_links(
        '{"links": [{"child_id": 1, "parent_name": "Acme", "cleaned_name": "Sub"}]}', {1}
    )
    assert links == [
        HierarchyLink(child_id=1, parent_id=None, parent_name="Acme", cleaned_name="Sub")
    ]


def test_parse_links_garbage_is_empty() -> None:
    assert _parse_links("no soy json", {1, 2}) == []
    assert _parse_links('{"otra": 1}', {1, 2}) == []


def test_parse_links_drops_unknown_child_and_parent() -> None:
    # child 9 fuera de valid_ids → descartado; parent 9 inexistente + sin nombre → XOR falla
    assert _parse_links('{"links": [{"child_id": 9, "parent_id": 1}]}', {1, 2}) == []
    assert _parse_links('{"links": [{"child_id": 1, "parent_id": 9}]}', {1, 2}) == []


def test_parse_links_xor_violations() -> None:
    # ni padre ni nombre
    assert _parse_links('{"links": [{"child_id": 1}]}', {1, 2}) == []
    # ambos a la vez
    both = '{"links": [{"child_id": 1, "parent_id": 2, "parent_name": "X"}]}'
    assert _parse_links(both, {1, 2}) == []


def test_parse_links_dedup_child_first_wins() -> None:
    raw = '{"links": [{"child_id": 1, "parent_id": 2}, {"child_id": 1, "parent_id": 3}]}'
    links = _parse_links(raw, {1, 2, 3})
    assert len(links) == 1 and links[0].parent_id == 2


# ----- would_create_cycle (DB) --------------------------------------------------- #


def test_would_create_cycle(conn: Any) -> None:
    a = _mk_org(conn, "A")
    b = _mk_org(conn, "B")
    conn.execute(
        text("UPDATE mod_identidades SET parent_identity_id = :a WHERE id = :b"), {"a": a, "b": b}
    )
    # B ya cuelga de A; colgar A de B cerraría el ciclo.
    assert would_create_cycle(conn, 1, a, b) is True
    # colgar B de A ya es el estado actual (sin ciclo nuevo).
    assert would_create_cycle(conn, 1, b, a) is False
    # self-parent siempre es ciclo.
    assert would_create_cycle(conn, 1, a, a) is True


# ----- _apply_links (DB) --------------------------------------------------------- #


def test_apply_links_by_id(conn: Any) -> None:
    child = _mk_org(conn, "Steam")
    parent = _mk_org(conn, "Valve Corporation")
    linked, created, cleaned, skipped = _apply_links(
        conn, 1, [HierarchyLink(child, parent, None, None)], apply_cleanup=False
    )
    assert (linked, created, cleaned, skipped) == (1, 0, 0, 0)
    assert _parent_of(conn, child) == parent


def test_apply_links_creates_parent_by_name_idempotent(conn: Any) -> None:
    c1 = _mk_org(conn, "Producto A")
    c2 = _mk_org(conn, "Producto B")
    # dos hijos al MISMO padre-por-nombre: se crea UNA sola org (la segunda la reusa por org_core).
    linked, created, _, _ = _apply_links(
        conn,
        1,
        [
            HierarchyLink(c1, None, "Empresa Matriz", None),
            HierarchyLink(c2, None, "Empresa Matriz", None),
        ],
        apply_cleanup=False,
    )
    assert (linked, created) == (2, 1)
    p1, p2 = _parent_of(conn, c1), _parent_of(conn, c2)
    assert p1 is not None and p1 == p2
    src = conn.execute(
        text("SELECT source, metadata->>'created_by' FROM mod_identidades WHERE id = :p"),
        {"p": p1},
    ).first()
    assert src is not None and src[0] == "extraction" and src[1] == "hierarchy_llm"


def test_resolve_reuses_existing_parent_by_core(conn: Any) -> None:
    existing = _mk_org(conn, "Valve Corporation")
    pid, created = _resolve_or_create_parent(conn, 1, "Valve")  # org_core('Valve')='valve'
    assert created is False and pid == existing


def test_apply_links_skips_cycle_and_self(conn: Any) -> None:
    a = _mk_org(conn, "Uno")
    b = _mk_org(conn, "Dos")
    # A→B y B→A en el mismo lote: el primero aplica, el segundo cerraría ciclo → se salta.
    linked, _, _, skipped = _apply_links(
        conn,
        1,
        [HierarchyLink(a, b, None, None), HierarchyLink(b, a, None, None)],
        apply_cleanup=False,
    )
    assert (linked, skipped) == (1, 1)
    # exactamente uno quedó con padre (sin ciclo en la DB).
    parents = [_parent_of(conn, a), _parent_of(conn, b)]
    assert parents.count(None) == 1


def test_apply_links_cleanup_off_then_on(conn: Any) -> None:
    parent = _mk_org(conn, "Universidad del Norte")
    child = _mk_org(conn, "Ingeniería Mecánica - Universidad del Norte")
    link = HierarchyLink(child, parent, None, "Ingeniería Mecánica")
    # OFF (default): no renombra.
    _, _, cleaned_off, _ = _apply_links(conn, 1, [link], apply_cleanup=False)
    assert cleaned_off == 0
    name = conn.execute(
        text("SELECT display_name FROM mod_identidades WHERE id = :c"), {"c": child}
    ).scalar_one()
    assert name == "Ingeniería Mecánica - Universidad del Norte"
    # ON: renombra al sub y deja el original como alias.
    _, _, cleaned_on, _ = _apply_links(conn, 1, [link], apply_cleanup=True)
    assert cleaned_on == 1
    row = conn.execute(
        text("SELECT display_name, aliases FROM mod_identidades WHERE id = :c"), {"c": child}
    ).first()
    assert row is not None
    assert row[0] == "Ingeniería Mecánica"
    assert "Ingeniería Mecánica - Universidad del Norte" in row[1]


# ----- run_organize (worker, LLM falso) ------------------------------------------ #


@pytest.mark.asyncio
async def test_run_organize_links_by_id() -> None:
    with connection() as c:
        steam = _mk_org(c, "Steam")
        valve = _mk_org(c, "Valve Corporation")
    fake = FakeLLM(f'{{"links": [{{"child_id": {steam}, "parent_id": {valve}}}]}}')
    stats = await run_organize(1, client=fake)
    assert (stats.orgs, stats.linked, stats.created) == (2, 1, 0)
    with connection() as c:
        assert _parent_of(c, steam) == valve


@pytest.mark.asyncio
async def test_run_organize_empty_no_call() -> None:
    fake = FakeLLM('{"links": []}')
    stats = await run_organize(1, client=fake)
    assert stats.orgs == 0 and fake.calls == 0  # sin orgs ni siquiera llama al LLM


@pytest.mark.asyncio
async def test_run_organize_quota_propagates() -> None:
    with connection() as c:
        _mk_org(c, "Una Org")
    with pytest.raises(LLMQuotaError):
        await run_organize(1, client=QuotaLLM())
