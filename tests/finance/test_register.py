"""`finance.register` determinista: inserta + resuelve identidad + dedup FASE 1 + `event_id`; y la
arista cross-module bienestar↔finanzas por `event_id` (vía el productor del grafo)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.db import connection
from memex.modules.bienestar.module import register as registrar_bienestar
from memex.modules.finance.consolidate import run_consolidation
from memex.modules.finance.module import register
from memex.relations.deterministic import build_relations
from memex.relations.edges import list_edges

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection


def test_register_inserts(conn: Connection) -> None:
    row = register(
        conn, 1, amount=Decimal("20.00"), currency="usd", counterparty="Pizzería", event_id="E1"
    )
    assert row["amount"] == 20.0
    assert row["currency"] == "USD"  # normalizado
    assert row["direction"] == "egreso"
    assert row["event_id"] == "E1"
    n = conn.execute(
        text("SELECT count(*) FROM mod_finance_transactions WHERE user_id = 1")
    ).scalar_one()
    assert n == 1


def test_register_resolves_identity(conn: Connection) -> None:
    oid = conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1,'organizacion','Rappi') RETURNING id"
        )
    ).scalar_one()
    register(conn, 1, amount=Decimal("100"), currency="COP", counterparty="Rappi")
    fk = conn.execute(
        text("SELECT counterparty_identity_id FROM mod_finance_transactions")
    ).scalar_one()
    assert fk == oid


def test_register_invalid_category_otros(conn: Connection) -> None:
    register(conn, 1, amount=Decimal("5"), currency="USD", category="inventada")
    cat = conn.execute(text("SELECT category FROM mod_finance_transactions")).scalar_one()
    assert cat == "otros"


def test_register_dedup_marks_pair(conn: Connection) -> None:
    # dos cargos iguales (mismo monto + hora + contraparte + lugar) → par de dedup FASE 1.
    when = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)
    for _ in range(2):
        register(
            conn,
            1,
            amount=Decimal("50"),
            currency="USD",
            counterparty="Uber",
            place="centro",
            occurred_at=when,
            occurred_at_precision="datetime",
        )
    pairs = conn.execute(
        text("SELECT count(*) FROM mod_finance_dedup_candidates WHERE user_id = 1")
    ).scalar_one()
    assert pairs == 1


def test_cross_module_same_event_edge_incremental() -> None:
    # un mensaje (event E7) → transacción (finanzas) + registro (bienestar). Tras consolidar hay una
    # arista bienestar↔finanzas SIN llamar build_relations: la consolidación de finanzas (donde nace
    # su vértice) la teje. Usa connection() propias porque run_consolidation abre la suya.
    with connection() as c:
        register(c, 1, amount=Decimal("20"), currency="USD", counterparty="Pizzería", event_id="E7")
        registrar_bienestar(c, 1, category="comida", activity="almuerzo", event_id="E7")
    run_consolidation(1)  # crea el consolidado (vértice de finanzas) Y teje sus aristas
    with connection() as c:
        edges = list_edges(c, 1, producer="event")
    slugs = {(e.src.slug, e.dst.slug) for e in edges}
    assert ("bienestar", "finance") in slugs  # tejida sin full-sweep
    for e in edges:
        assert e.relation_type == "mismo_evento"
        assert e.status == "confirmed"


def test_full_sweep_idempotent_after_incremental() -> None:
    # el full-sweep sigue siendo respaldo: re-correrlo sobre lo ya tejido no duplica.
    with connection() as c:
        register(c, 1, amount=Decimal("20"), currency="USD", counterparty="Pizzería", event_id="E7")
        registrar_bienestar(c, 1, category="comida", activity="almuerzo", event_id="E7")
    run_consolidation(1)
    with connection() as c:
        before = len(list_edges(c, 1, producer="event"))
        build_relations(c, 1)
        after = len(list_edges(c, 1, producer="event"))
    assert before == 1
    assert after == 1


def test_contraparte_edge_incremental() -> None:
    # la arista «contraparte» (consolidado → identidad) también se teje en la consolidación, sin
    # build_relations.
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'organizacion','Rappi')"
            )
        )
        register(c, 1, amount=Decimal("100"), currency="COP", counterparty="Rappi")
    run_consolidation(1)
    with connection() as c:
        edges = list_edges(c, 1, producer="finance")
    assert any(e.relation_type == "contraparte" and e.src.slug == "finance" for e in edges)
