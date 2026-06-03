from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import text

from memex.db import connection


def _seed_expense(
    user_id: int,
    *,
    amount: float = 100.0,
    currency: str = "MXN",
    category: str = "comida",
    merchant: str = "OXXO",
    occurred_on: date | None = date(2026, 5, 10),
    description: str = "",
    evidence: str = "",
    source_inbox_ids: list[int] | None = None,
) -> None:
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_finance_expenses
                  (user_id, source_inbox_ids, amount, currency, category, merchant,
                   occurred_on, description, evidence)
                VALUES
                  (:uid, :ids, :amount, :currency, :category, :merchant,
                   :occurred_on, :description, :evidence)
                """
            ),
            {
                "uid": user_id,
                "ids": source_inbox_ids if source_inbox_ids is not None else [1],
                "amount": amount,
                "currency": currency,
                "category": category,
                "merchant": merchant,
                "occurred_on": occurred_on,
                "description": description,
                "evidence": evidence,
            },
        )


def test_list_expenses_returns_user_rows(client: Any) -> None:
    _seed_expense(1, amount=42.5, currency="MXN", source_inbox_ids=[7, 8])
    _seed_expense(1, amount=10.0, currency="USD")
    r = client.get("/finance/expenses")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is None
    first = body["items"][0]
    assert first["amount"] == 42.5  # NUMERIC → float
    assert isinstance(first["amount"], float)
    assert first["source_inbox_ids"] == [7, 8]
    assert first["currency"] == "MXN"
    assert first["occurred_on"] == "2026-05-10"


def test_list_expenses_cross_tenant_scoped(client: Any, seed_user2: int) -> None:
    _seed_expense(1, merchant="mine")
    _seed_expense(seed_user2, merchant="theirs")
    items = client.get("/finance/expenses").json()["items"]
    assert len(items) == 1
    assert items[0]["merchant"] == "mine"


def test_list_expenses_filter_by_currency(client: Any) -> None:
    # gastos distintos (monto distinto) → el dedup v2 no los colapsa
    _seed_expense(1, currency="MXN", amount=1.0)
    _seed_expense(1, currency="USD", amount=2.0)
    _seed_expense(1, currency="USD", amount=3.0)
    assert len(client.get("/finance/expenses?currency=USD").json()["items"]) == 2
    assert len(client.get("/finance/expenses?currency=MXN").json()["items"]) == 1


def test_list_expenses_filter_by_date_range(client: Any) -> None:
    _seed_expense(1, occurred_on=date(2026, 3, 15))
    _seed_expense(1, occurred_on=date(2026, 4, 15))
    _seed_expense(1, occurred_on=date(2026, 5, 15))
    # since inclusive, until exclusive → only April
    items = client.get("/finance/expenses?since=2026-04-01&until=2026-05-01").json()["items"]
    assert len(items) == 1
    assert items[0]["occurred_on"] == "2026-04-15"


def test_list_expenses_pagination(client: Any) -> None:
    for i in range(5):  # montos distintos → 5 vértices distintos (no colapsan)
        _seed_expense(1, amount=float(100 + i))
    body1 = client.get("/finance/expenses?limit=2").json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None
    body2 = client.get(f"/finance/expenses?limit=2&cursor={body1['next_cursor']}").json()
    assert len(body2["items"]) == 2
    assert body2["items"][0]["id"] > body1["items"][-1]["id"]


def test_list_expenses_null_occurred_on(client: Any) -> None:
    _seed_expense(1, occurred_on=None)
    items = client.get("/finance/expenses").json()["items"]
    assert len(items) == 1
    assert items[0]["occurred_on"] is None


def test_list_expenses_empty(client: Any) -> None:
    body = client.get("/finance/expenses").json()
    assert body == {"items": [], "next_cursor": None}
