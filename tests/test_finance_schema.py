"""Normalización del nuevo `category` (rubro de lista cerrada) + `currency` en ExpenseItem."""

from __future__ import annotations

from memex.modules.finance.schema import ExpenseItem


def _item(**over: object) -> ExpenseItem:
    base: dict[str, object] = {
        "source_inbox_ids": (1,),
        "amount": "10",
        "currency": "usd",
        "merchant": "X",
    }
    base.update(over)
    return ExpenseItem(**base)


def test_unknown_category_falls_back_to_otros() -> None:
    assert _item(category="food").category == "otros"


def test_valid_category_normalizes_case() -> None:
    assert _item(category="Comida").category == "comida"


def test_category_defaults_when_missing() -> None:
    assert _item().category == "otros"


def test_currency_uppercased() -> None:
    assert _item(currency="cop").currency == "COP"
