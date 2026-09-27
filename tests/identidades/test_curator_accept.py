"""Curador: `set_kind` (retipar producto↔organización, con la política de finanzas: lo que cobra
es organización) y `rounds` (repetir la pasada árbol hasta que una ronda no cambie nada)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.curator import (
    CuratorStats,
    apply_decisions,
    collect_items,
    parse_decisions,
    run_curator,
)
from memex.webcontext._codex_subprocess import CodexRun
from memex.webcontext.client import WebContextUsage


def _mk(conn: Any, kind: str, name: str, parent: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, parent_identity_id) "
                "VALUES (1, :k, :n, :p) RETURNING id"
            ),
            {"k": kind, "n": name, "p": parent},
        ).scalar_one()
    )


def _decision(key: str, action: str, **kw: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "item": key,
        "action": action,
        "parent_id": None,
        "parent_name": None,
        "survivor_id": None,
        "absorbed_id": None,
        "kind": None,
        "confidence": 0.95,
        "rationale": "r",
    }
    d.update(kw)
    return d


def test_set_kind_retipa_aunque_cobre(conn: Any) -> None:
    valve = _mk(conn, "organizacion", "Valve Software")
    steam = _mk(conn, "organizacion", "Steam", parent=valve)
    cursor = _mk(conn, "organizacion", "Cursor")
    # Steam cobra: hay una transacción a su nombre → igual se retipa (un producto puede cobrar)
    conn.execute(
        text(
            "INSERT INTO mod_finance_transactions (user_id, source_inbox_ids, direction, amount, "
            "currency, category, counterparty, counterparty_identity_id, occurred_at, evidence) "
            "VALUES (1, ARRAY[1]::bigint[], 'egreso', 10, 'COP', 'entretenimiento', 'Steam', :s, "
            "NOW(), 'x')"
        ),
        {"s": steam},
    )
    items = collect_items(conn, 1, scope="tree")
    by = {it.identity_id: it for it in items}
    content = json.dumps(
        {
            "decisions": [
                _decision(by[cursor].key, "set_kind", kind="producto"),
                _decision(by[steam].key, "set_kind", kind="producto"),
                _decision(by[valve].key, "set_kind", kind="organizacion"),  # ya lo es
            ]
        }
    )
    stats = CuratorStats()
    apply_decisions(conn, 1, items, parse_decisions(content, items), stats)
    assert stats.retyped == 2
    results = {a["item"]: a["result"] for a in stats.applied}
    assert results[by[cursor].key] == "retyped"
    assert results[by[steam].key] == "retyped"
    assert results[by[valve].key] == "dropped:kind_same"
    kinds = dict(
        conn.execute(
            text("SELECT id, kind FROM mod_identidades WHERE id = ANY(:ids)"),
            {"ids": [cursor, steam]},
        ).all()
    )
    assert kinds == {cursor: "producto", steam: "producto"}
    # la transacción sigue atada a Steam (ahora producto): finanzas ya no veta productos
    fk = conn.execute(
        text("SELECT counterparty_identity_id FROM mod_finance_transactions LIMIT 1")
    ).scalar_one()
    assert fk == steam
    # una decisión con kind fuera del contrato se parsea sin kind → descartada con razón
    bad = json.dumps({"decisions": [_decision(by[cursor].key, "set_kind", kind="persona")]})
    stats = CuratorStats()
    apply_decisions(conn, 1, items, parse_decisions(bad, items), stats)
    assert stats.applied[0]["result"] == "dropped:kind_value"


def _run(content: str) -> CodexRun:
    return CodexRun(
        last_message=content, usage=WebContextUsage(input_tokens=1, output_tokens=1), latency_ms=1
    )


@pytest.mark.asyncio
async def test_rounds_repite_hasta_que_nada_cambie() -> None:
    with connection() as c:
        acme = _mk(c, "organizacion", "Acme")
        prod = _mk(c, "organizacion", "Acme Cloud")
    calls: list[int] = []

    async def ask(prompt: str) -> CodexRun:
        calls.append(len(calls) + 1)
        if len(calls) == 1:  # 1ª ronda: cuelga el producto
            return _run(
                json.dumps({"decisions": [_decision(f"node:{prod}", "set_parent", parent_id=acme)]})
            )
        return _run(json.dumps({"decisions": []}))  # 2ª ronda: nada → se detiene

    stats = await run_curator(1, scope="tree", rounds=3, ask=ask)
    assert stats.rounds == 2 and len(calls) == 2
    assert stats.set == 1 and all("round" in a for a in stats.applied)
    with connection() as c:
        parent = c.execute(
            text("SELECT parent_identity_id FROM mod_identidades WHERE id = :p"), {"p": prod}
        ).scalar()
    assert parent == acme
    # dry-run: una sola ronda aunque se pidan más
    calls.clear()
    stats = await run_curator(1, scope="tree", rounds=3, dry_run=True, ask=ask)
    assert stats.rounds == 1 and len(calls) == 1
