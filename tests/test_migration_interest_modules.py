"""Schema check para las tablas de ADR-015 (migración 0008).

No hay consumidor en esta capa (el orquestador se testea aparte): verificamos que el DDL
impone lo diseñado — UNIQUEs de habilitación e idempotencia, CHECK de batching_policy,
cascadas hacia users/inbox, y el array de atribución de mod_finance_expenses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection


def _seed_inbox(source_id: int, external_id: str) -> int:
    with connection() as c:
        inbox_id = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occurred, CAST(:payload AS JSONB))
                RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": external_id,
                "occurred": datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
                "payload": "{}",
            },
        ).scalar()
    assert inbox_id is not None
    return int(inbox_id)


# ----- module_settings ----------------------------------------------------------- #


def test_module_settings_insert_defaults() -> None:
    with connection() as c:
        row = (
            c.execute(
                text(
                    "INSERT INTO module_settings (user_id, module_slug) "
                    "VALUES (1, 'finance') RETURNING enabled, batching_policy"
                )
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["enabled"] is False
    assert row["batching_policy"] == "per_module"


def test_module_settings_unique_per_user_module() -> None:
    with connection() as c:
        c.execute(text("INSERT INTO module_settings (user_id, module_slug) VALUES (1, 'finance')"))
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(text("INSERT INTO module_settings (user_id, module_slug) VALUES (1, 'finance')"))


def test_module_settings_rejects_unknown_policy() -> None:
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, batching_policy) "
                "VALUES (1, 'finance', 'nonsense')"
            )
        )


def test_module_settings_enable_is_upsert() -> None:
    """Habilitar = upsert sobre UNIQUE(user_id, module_slug)."""
    sql = text(
        """
        INSERT INTO module_settings (user_id, module_slug, enabled)
        VALUES (1, 'finance', TRUE)
        ON CONFLICT (user_id, module_slug) DO UPDATE SET enabled = TRUE
        RETURNING enabled
        """
    )
    with connection() as c:
        c.execute(sql)
        second = c.execute(sql).scalar()
        n = c.execute(
            text(
                "SELECT count(*) FROM module_settings WHERE user_id = 1 AND module_slug = 'finance'"
            )
        ).scalar()
    assert second is True
    assert n == 1


def test_module_settings_cascade_on_user_delete(seed_user2: int) -> None:
    with connection() as c:
        c.execute(
            text("INSERT INTO module_settings (user_id, module_slug) VALUES (:u, 'finance')"),
            {"u": seed_user2},
        )
        c.execute(text("DELETE FROM users WHERE id = :u"), {"u": seed_user2})
        remaining = c.execute(
            text("SELECT count(*) FROM module_settings WHERE user_id = :u"), {"u": seed_user2}
        ).scalar()
    assert remaining == 0


# ----- module_extractions -------------------------------------------------------- #


def test_module_extractions_unique_per_module_inbox(seed_source: dict[str, Any]) -> None:
    iid = _seed_inbox(seed_source["id"], "m1")
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
                "VALUES (1, 'finance', :iid)"
            ),
            {"iid": iid},
        )
    with pytest.raises(IntegrityError), connection() as c:
        c.execute(
            text(
                "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
                "VALUES (1, 'finance', :iid)"
            ),
            {"iid": iid},
        )


def test_module_extractions_on_conflict_do_nothing(seed_source: dict[str, Any]) -> None:
    """El cursor se inserta idempotente: segundo intento no rompe ni duplica."""
    iid = _seed_inbox(seed_source["id"], "m1")
    sql = text(
        "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
        "VALUES (1, 'finance', :iid) ON CONFLICT (module_slug, inbox_id) DO NOTHING"
    )
    with connection() as c:
        c.execute(sql, {"iid": iid})
        c.execute(sql, {"iid": iid})
        n = c.execute(text("SELECT count(*) FROM module_extractions")).scalar()
    assert n == 1


def test_module_extractions_distinct_modules_same_inbox(seed_source: dict[str, Any]) -> None:
    """Dos módulos distintos pueden extraer del mismo mensaje (UNIQUE es por par)."""
    iid = _seed_inbox(seed_source["id"], "m1")
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
                "VALUES (1, 'finance', :iid), (1, 'calendar', :iid)"
            ),
            {"iid": iid},
        )
        n = c.execute(
            text("SELECT count(*) FROM module_extractions WHERE inbox_id = :iid"), {"iid": iid}
        ).scalar()
    assert n == 2


def test_module_extractions_cascade_on_inbox_delete(seed_source: dict[str, Any]) -> None:
    iid = _seed_inbox(seed_source["id"], "m1")
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_extractions (user_id, module_slug, inbox_id) "
                "VALUES (1, 'finance', :iid)"
            ),
            {"iid": iid},
        )
        c.execute(text("DELETE FROM inbox WHERE id = :iid"), {"iid": iid})
        remaining = c.execute(
            text("SELECT count(*) FROM module_extractions WHERE inbox_id = :iid"), {"iid": iid}
        ).scalar()
    assert remaining == 0


# ----- mod_finance_transactions (atribución por-mensaje + cascada) ---------------- #


def test_finance_transaction_insert_and_read_back(seed_source: dict[str, Any]) -> None:
    i1 = _seed_inbox(seed_source["id"], "m1")
    i2 = _seed_inbox(seed_source["id"], "m2")
    with connection() as c:
        row = (
            c.execute(
                text(
                    """
                    INSERT INTO mod_finance_transactions
                      (user_id, source_inbox_ids, direction, amount, currency, counterparty,
                       occurred_at, description, evidence)
                    VALUES (1, :ids, 'egreso', 4500.00, 'ARS', 'Edenor', NOW(),
                            'pago de luz', 'pagué los $4.500 de la luz')
                    RETURNING amount, currency, counterparty, source_inbox_ids
                    """
                ),
                {"ids": [i1, i2]},
            )
            .mappings()
            .first()
        )
    assert row is not None
    assert row["currency"] == "ARS"
    assert row["counterparty"] == "Edenor"
    assert list(row["source_inbox_ids"]) == [i1, i2]


def test_finance_transaction_cascade_on_user_delete(
    seed_user2: int, seed_source: dict[str, Any]
) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_transactions (user_id, source_inbox_ids, direction, "
                "amount, currency, occurred_at) "
                "VALUES (:u, ARRAY[]::bigint[], 'egreso', 1.00, 'USD', NOW())"
            ),
            {"u": seed_user2},
        )
        c.execute(text("DELETE FROM users WHERE id = :u"), {"u": seed_user2})
        remaining = c.execute(
            text("SELECT count(*) FROM mod_finance_transactions WHERE user_id = :u"),
            {"u": seed_user2},
        ).scalar()
    assert remaining == 0
