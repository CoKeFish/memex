"""`finance.register` determinista: inserta + identidad + dedup FASE 1 + `event_id`; asegura el
CONSOLIDADO (vértice de finanzas) al ESCRIBIR y teje sus aristas — sin consolidación batch."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import text

from memex.db import connection
from memex.modules.bienestar.module import register as registrar_bienestar
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


def test_register_creates_consolidated(conn: Connection) -> None:
    # factura sola (sin evento): el consolidado (vértice de finanzas) nace al registrar, sin batch.
    row = register(conn, 1, amount=Decimal("42500"), currency="COP", counterparty="Crepes")
    cons = conn.execute(
        text(
            "SELECT c.id FROM mod_finance_consolidated c "
            "JOIN mod_finance_transaction_links l ON l.consolidated_id = c.id "
            "WHERE l.transaction_id = :t AND NOT c.deleted"
        ),
        {"t": int(row["id"])},
    ).scalar_one()
    assert cons is not None
    assert list_edges(conn, 1, producer="event") == []  # sin evento → ninguna arista de evento


def test_cross_module_same_event_edge_at_register() -> None:
    # un mensaje (event E7) → transacción (finanzas) + registro (bienestar). La arista bienestar↔
    # finanzas se teje al ESCRIBIR (sin run_consolidation ni build_relations): finance.register
    # asegura su consolidado y el último en aterrizar teje el mismo_evento. connection() propia para
    # commitear antes de releer.
    with connection() as c:
        register(c, 1, amount=Decimal("20"), currency="USD", counterparty="Pizzería", event_id="E7")
        registrar_bienestar(c, 1, category="comida", activity="almuerzo", event_id="E7")
    with connection() as c:
        edges = list_edges(c, 1, producer="event")
    slugs = {(e.src.slug, e.dst.slug) for e in edges}
    assert ("bienestar", "finance") in slugs
    for e in edges:
        assert e.relation_type == "mismo_evento"
        assert e.status == "confirmed"


def test_full_sweep_idempotent_after_incremental() -> None:
    # el full-sweep (build_relations) sigue de respaldo: sobre lo ya tejido al escribir, no duplica.
    with connection() as c:
        register(c, 1, amount=Decimal("20"), currency="USD", counterparty="Pizzería", event_id="E7")
        registrar_bienestar(c, 1, category="comida", activity="almuerzo", event_id="E7")
    with connection() as c:
        before = len(list_edges(c, 1, producer="event"))
        build_relations(c, 1)
        after = len(list_edges(c, 1, producer="event"))
    assert before == 1
    assert after == 1


def test_contraparte_edge_at_register(conn: Connection) -> None:
    # «contraparte» (consolidado→identidad) se teje al registrar (sin batch) si hay identidad.
    conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1,'organizacion','Rappi')"
        )
    )
    register(conn, 1, amount=Decimal("100"), currency="COP", counterparty="Rappi")
    edges = list_edges(conn, 1, producer="finance")
    assert any(e.relation_type == "contraparte" and e.src.slug == "finance" for e in edges)


def test_register_autoconfirmed_dup_one_consolidated(conn: Connection) -> None:
    # dos cargos idénticos (auto-confirmados en FASE 1) → UN consolidado vivo, ambas tx linkeadas:
    # la 2ª se UNE al consolidado de la 1ª, no crea un vértice duplicado.
    when = datetime(2026, 6, 5, 14, 30, tzinfo=UTC)
    ids = [
        int(
            register(
                conn,
                1,
                amount=Decimal("50"),
                currency="USD",
                counterparty="Uber",
                place="centro",
                occurred_at=when,
                occurred_at_precision="datetime",
            )["id"]
        )
        for _ in range(2)
    ]
    live = conn.execute(
        text("SELECT count(*) FROM mod_finance_consolidated WHERE user_id = 1 AND NOT deleted")
    ).scalar_one()
    assert live == 1
    cons_ids = {
        int(r[0])
        for r in conn.execute(
            text(
                "SELECT DISTINCT consolidated_id FROM mod_finance_transaction_links "
                "WHERE transaction_id = ANY(:ids)"
            ),
            {"ids": ids},
        ).all()
    }
    assert len(cons_ids) == 1  # ambas tx en el mismo consolidado
