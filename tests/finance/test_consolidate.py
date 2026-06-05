"""Consolidación de finance: la parte PURA (`build_groups`/`pick_winner`/`merge_fields`) +
`run_consolidation` (DB, estable e idempotente)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text

from memex.db import connection
from memex.modules.finance.consolidate import (
    ConsTx,
    build_groups,
    merge_fields,
    pick_winner,
    run_consolidation,
)

_AT = datetime(2026, 6, 3, 14, 0, tzinfo=UTC)
_AT2 = datetime(2026, 6, 4, 9, 0, tzinfo=UTC)


def _cons(
    tid: int,
    *,
    counterparty: str = "",
    place: str = "",
    description: str = "",
    category: str = "otros",
    precision: str = "datetime",
    occurred_at: datetime | None = None,
) -> ConsTx:
    return ConsTx(
        transaction_id=tid,
        direction="egreso",
        amount=Decimal("100"),
        currency="USD",
        category=category,
        counterparty=counterparty,
        place=place,
        occurred_at=occurred_at if occurred_at is not None else _AT,
        precision=precision,
        description=description,
        recency=datetime(2026, 6, 3, tzinfo=UTC),
    )


# ----- puro ---------------------------------------------------------------------- #


def test_build_groups_transitive() -> None:
    assert build_groups([1, 2, 3], [(1, 2), (2, 3)]) == [[1, 2, 3]]


def test_build_groups_disjoint() -> None:
    assert build_groups([1, 2, 3, 4], [(1, 2)]) == [[1, 2], [3], [4]]


def test_pick_winner_most_complete() -> None:
    bare = _cons(1)
    full = _cons(2, counterparty="Rappi", place="Calle 1", description="x", category="comida")
    assert pick_winner([bare, full]).transaction_id == 2


def test_pick_winner_prefers_known_date_over_inferred() -> None:
    inferred = _cons(1, precision="inferred")
    dated = _cons(2, precision="date")
    assert pick_winner([inferred, dated]).transaction_id == 2


def test_merge_fields_fill_only() -> None:
    winner = _cons(3, counterparty="Rappi", description="compra", category="comida")
    filler = _cons(1, place="Calle 1")
    merged = merge_fields([winner, filler])
    assert merged.winner_transaction_id == 3
    assert merged.counterparty == "Rappi"
    assert merged.place == "Calle 1"  # rellenado desde el otro miembro


def test_merge_fields_adopts_better_date() -> None:
    winner = _cons(
        2,
        counterparty="R",
        place="P",
        description="d",
        category="comida",
        precision="inferred",
        occurred_at=_AT,
    )
    member = _cons(1, precision="date", occurred_at=_AT2)
    merged = merge_fields([winner, member])
    assert merged.winner_transaction_id == 2  # gana por completitud
    assert merged.precision == "date"  # pero adopta la mejor fecha del grupo
    assert merged.occurred_at == _AT2


# ----- DB ------------------------------------------------------------------------ #


def _seed_tx(*, amount: str = "100", counterparty: str = "Rappi") -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_finance_transactions "
                    "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, "
                    " occurred_at_precision, counterparty) "
                    "VALUES (1, ARRAY[]::bigint[], 'egreso', :amt, 'USD', :at, 'datetime', :cp) "
                    "RETURNING id"
                ),
                {"amt": Decimal(amount), "at": _AT, "cp": counterparty},
            ).scalar_one()
        )


def _seed_pair(a_id: int, b_id: int, *, status: str) -> None:
    lo, hi = min(a_id, b_id), max(a_id, b_id)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_dedup_candidates "
                "(user_id, transaction_a_id, transaction_b_id, reason, score, status) "
                "VALUES (1, :a, :b, 'amount+hora', 0.9, :st)"
            ),
            {"a": lo, "b": hi, "st": status},
        )


def _live_consolidated() -> int:
    with connection() as c:
        return int(
            c.execute(
                text("SELECT count(*) FROM mod_finance_consolidated WHERE NOT deleted")
            ).scalar_one()
        )


def test_singletons_each_consolidated() -> None:
    _seed_tx(amount="100")
    _seed_tx(amount="200")
    stats = run_consolidation(1)
    assert (stats.groups, stats.consolidated) == (2, 2)
    assert _live_consolidated() == 2


def test_confirmed_pair_collapses_to_one() -> None:
    a, b = _seed_tx(), _seed_tx()
    _seed_pair(a, b, status="confirmed")
    run_consolidation(1)
    assert _live_consolidated() == 1
    with connection() as c:
        links = c.execute(text("SELECT count(*) FROM mod_finance_transaction_links")).scalar_one()
        outcomes = sorted(
            r[0]
            for r in c.execute(
                text("SELECT processing_outcome FROM mod_finance_transactions ORDER BY id")
            ).all()
        )
    assert links == 2
    assert outcomes == ["duplicate", "unique"]  # uno gana (unique), el otro duplicate


def test_consolidation_idempotent_stable_id() -> None:
    a, b = _seed_tx(), _seed_tx()
    _seed_pair(a, b, status="confirmed")
    run_consolidation(1)
    with connection() as c:
        cid1 = c.execute(
            text("SELECT id FROM mod_finance_consolidated WHERE NOT deleted")
        ).scalar_one()
    run_consolidation(1)
    with connection() as c:
        cid2 = c.execute(
            text("SELECT id FROM mod_finance_consolidated WHERE NOT deleted")
        ).scalar_one()
    assert cid1 == cid2


def test_pending_pair_keeps_pending_outcome() -> None:
    a, b = _seed_tx(), _seed_tx()
    _seed_pair(a, b, status="candidate")  # aún sin resolver
    run_consolidation(1)
    with connection() as c:
        outcomes = {
            r[0]
            for r in c.execute(
                text("SELECT processing_outcome FROM mod_finance_transactions")
            ).all()
        }
    assert outcomes == {"pending"}  # no se les fija outcome mientras haya un par 'candidate'
