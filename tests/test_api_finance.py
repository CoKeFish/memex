from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _seed_consolidated(
    user_id: int,
    *,
    direction: str = "egreso",
    amount: float = 100.0,
    currency: str = "MXN",
    category: str = "comida",
    counterparty: str = "OXXO",
    place: str = "",
    occurred_at: datetime = datetime(2026, 5, 10, 12, 0, tzinfo=UTC),
    precision: str = "datetime",
    deleted: bool = False,
) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_finance_consolidated
                  (user_id, direction, amount, currency, category, counterparty, place,
                   occurred_at, occurred_at_precision, description, deleted)
                VALUES
                  (:uid, :dir, :amt, :cur, :cat, :cp, :place, :at, :prec, '', :deleted)
                """
            ),
            {
                "uid": user_id,
                "dir": direction,
                "amt": amount,
                "cur": currency,
                "cat": category,
                "cp": counterparty,
                "place": place,
                "at": occurred_at,
                "prec": precision,
                "deleted": deleted,
            },
        )


def test_list_transactions_returns_user_rows(client: Any) -> None:
    _seed_consolidated(1, amount=42.5, currency="MXN", direction="ingreso")
    _seed_consolidated(1, amount=10.0, currency="USD")
    r = client.get("/finance/transactions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert first["amount"] == 42.5  # NUMERIC → float
    assert isinstance(first["amount"], float)
    assert first["direction"] == "ingreso"
    assert first["currency"] == "MXN"
    assert first["occurred_at_precision"] == "datetime"


def test_list_transactions_cross_tenant_scoped(client: Any, seed_user2: int) -> None:
    _seed_consolidated(1, counterparty="mine")
    _seed_consolidated(seed_user2, counterparty="theirs")
    items = client.get("/finance/transactions").json()["items"]
    assert len(items) == 1
    assert items[0]["counterparty"] == "mine"


def test_list_transactions_excludes_deleted(client: Any) -> None:
    _seed_consolidated(1, counterparty="vivo")
    _seed_consolidated(1, counterparty="tombstone", deleted=True)
    items = client.get("/finance/transactions").json()["items"]
    assert len(items) == 1
    assert items[0]["counterparty"] == "vivo"


def test_list_transactions_filter_by_currency(client: Any) -> None:
    _seed_consolidated(1, currency="MXN", amount=1.0)
    _seed_consolidated(1, currency="USD", amount=2.0)
    _seed_consolidated(1, currency="USD", amount=3.0)
    assert len(client.get("/finance/transactions?currency=USD").json()["items"]) == 2
    assert len(client.get("/finance/transactions?currency=MXN").json()["items"]) == 1


def test_list_transactions_filter_by_direction(client: Any) -> None:
    _seed_consolidated(1, direction="ingreso", amount=1.0)
    _seed_consolidated(1, direction="egreso", amount=2.0)
    assert len(client.get("/finance/transactions?direction=ingreso").json()["items"]) == 1


def test_list_transactions_filter_by_date_range(client: Any) -> None:
    _seed_consolidated(1, occurred_at=datetime(2026, 3, 15, tzinfo=UTC))
    _seed_consolidated(1, occurred_at=datetime(2026, 4, 15, tzinfo=UTC))
    _seed_consolidated(1, occurred_at=datetime(2026, 5, 15, tzinfo=UTC))
    items = client.get("/finance/transactions?since=2026-04-01&until=2026-05-01").json()["items"]
    assert len(items) == 1


def test_list_transactions_pagination(client: Any) -> None:
    for i in range(5):
        _seed_consolidated(1, amount=float(100 + i))
    body1 = client.get("/finance/transactions?limit=2").json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    body2 = client.get(f"/finance/transactions?limit=2&cursor={body1['next_cursor']}").json()
    assert len(body2["items"]) == 2
    assert body2["items"][0]["id"] > body1["items"][-1]["id"]


def test_list_transactions_empty(client: Any) -> None:
    body = client.get("/finance/transactions").json()
    assert body == {"items": [], "next_cursor": None}
