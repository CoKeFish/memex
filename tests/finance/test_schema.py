"""Tests del schema `TransactionItem`: dirección con fallback, normalizaciones, extra prohibido."""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest
from pydantic import ValidationError

from memex.modules.contract import ExtractionItem
from memex.modules.finance.schema import TransactionItem


def _item(**over: object) -> TransactionItem:
    base: dict[str, object] = {"source_inbox_ids": (1,), "amount": "10", "currency": "usd"}
    base.update(over)
    return TransactionItem(**base)


def test_valid_with_coercion() -> None:
    t = TransactionItem(
        source_inbox_ids=[7],  # coerción list → tuple
        direction="ingreso",
        amount="12300",  # coerción str → Decimal
        currency="ARS",
        counterparty="Carrefour",
        place="Av. 9 #123",
        occurred_on="2026-06-03",  # coerción str → date
        occurred_time="14:30",  # coerción str → time
        description="transferencia recibida",
        evidence="Te transfirieron $12.300",
    )
    assert t.amount == Decimal("12300")
    assert t.occurred_on == date(2026, 6, 3)
    assert t.occurred_time == time(14, 30)
    assert t.direction == "ingreso"
    assert t.source_inbox_ids == (7,)


def test_direction_defaults_egreso_when_missing() -> None:
    assert _item().direction == "egreso"


def test_direction_normalizes_synonyms() -> None:
    assert _item(direction="income").direction == "ingreso"
    assert _item(direction="Ingreso").direction == "ingreso"
    assert _item(direction="abono").direction == "ingreso"


def test_direction_unknown_falls_back_to_egreso() -> None:
    # no se descarta el item por una dirección rara (igual que category → otros).
    assert _item(direction="cualquier-cosa").direction == "egreso"


def test_unknown_category_falls_back_to_otros() -> None:
    assert _item(category="food").category == "otros"


def test_valid_category_normalizes_case() -> None:
    assert _item(category="Comida").category == "comida"


def test_currency_uppercased() -> None:
    assert _item(currency="cop").currency == "COP"


def test_date_and_place_optional() -> None:
    t = _item()
    assert t.occurred_on is None
    assert t.occurred_time is None
    assert t.place == ""
    assert t.counterparty == ""


def test_forbids_extra_field() -> None:
    with pytest.raises(ValidationError):
        _item(merchant="X")  # el campo viejo `merchant` ya no existe → prohibido


def test_is_extraction_item() -> None:
    assert issubclass(TransactionItem, ExtractionItem)
