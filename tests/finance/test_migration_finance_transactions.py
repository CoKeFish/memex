"""Migración 0036 (finance v2): tablas nuevas, tabla vieja dropeada, CHECKs y cascadas."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import exc, text

from memex.db import connection

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

_INSERT_TX = (
    "INSERT INTO mod_finance_transactions "
    "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, occurred_at_precision) "
    "VALUES (1, ARRAY[]::bigint[], :dir, 1, 'USD', NOW(), :prec) RETURNING id"
)


def _exists(table: str) -> bool:
    with connection() as c:
        return c.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None


def test_new_tables_exist() -> None:
    for t in (
        "mod_finance_transactions",
        "mod_finance_dedup_candidates",
        "mod_finance_consolidated",
        "mod_finance_transaction_links",
    ):
        assert _exists(t), t


def test_old_expenses_table_dropped() -> None:
    assert not _exists("mod_finance_expenses")


def test_direction_check_rejects_bad_value(conn: Connection) -> None:
    with pytest.raises(exc.IntegrityError):
        conn.execute(text(_INSERT_TX), {"dir": "foo", "prec": "datetime"})


def test_precision_check_rejects_bad_value(conn: Connection) -> None:
    with pytest.raises(exc.IntegrityError):
        conn.execute(text(_INSERT_TX), {"dir": "egreso", "prec": "nope"})


def test_candidate_requires_a_less_than_b(conn: Connection) -> None:
    a = conn.execute(text(_INSERT_TX), {"dir": "egreso", "prec": "datetime"}).scalar_one()
    b = conn.execute(text(_INSERT_TX), {"dir": "egreso", "prec": "datetime"}).scalar_one()
    with pytest.raises(exc.IntegrityError):  # CHECK (transaction_a_id < transaction_b_id)
        conn.execute(
            text(
                "INSERT INTO mod_finance_dedup_candidates "
                "(user_id, transaction_a_id, transaction_b_id, reason) VALUES (1, :hi, :lo, 'x')"
            ),
            {"hi": b, "lo": a},
        )


def test_delete_transaction_cascades_candidates(conn: Connection) -> None:
    a = conn.execute(text(_INSERT_TX), {"dir": "egreso", "prec": "datetime"}).scalar_one()
    b = conn.execute(text(_INSERT_TX), {"dir": "egreso", "prec": "datetime"}).scalar_one()
    conn.execute(
        text(
            "INSERT INTO mod_finance_dedup_candidates "
            "(user_id, transaction_a_id, transaction_b_id, reason) VALUES (1, :a, :b, 'x')"
        ),
        {"a": a, "b": b},
    )
    conn.execute(text("DELETE FROM mod_finance_transactions WHERE id = :id"), {"id": a})
    remaining = conn.execute(text("SELECT count(*) FROM mod_finance_dedup_candidates")).scalar_one()
    assert remaining == 0
