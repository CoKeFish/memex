"""Tests puros del módulo finance: schema ExpenseItem, registry y disciplina de Protocol."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from memex.modules import known_modules, resolve
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, InterestModule
from memex.modules.finance.module import FinanceModule
from memex.modules.finance.schema import ExpenseItem


def test_expense_item_valid_with_coercion() -> None:
    e = ExpenseItem(
        source_inbox_ids=[7],  # coerción list → tuple
        amount="12300",  # coerción str → Decimal
        currency="ARS",
        merchant="Carrefour",
        occurred_on="2026-06-03",  # coerción str → date
        description="consumo tarjeta",
        evidence="Consumo $12.300 en Carrefour",
    )
    assert e.amount == Decimal("12300")
    assert e.occurred_on == date(2026, 6, 3)
    assert e.source_inbox_ids == (7,)


def test_expense_item_occurred_on_optional() -> None:
    e = ExpenseItem(source_inbox_ids=(1,), amount=Decimal("1"), currency="$", merchant="x")
    assert e.occurred_on is None
    assert e.description == ""


def test_expense_item_forbids_extra() -> None:
    with pytest.raises(ValidationError):
        ExpenseItem(
            source_inbox_ids=(1,),
            amount=Decimal("1"),
            currency="$",
            merchant="x",
            categoria="gasto",  # type: ignore[call-arg]  # extra prohibido
        )


def test_expense_item_is_extraction_item() -> None:
    assert issubclass(ExpenseItem, ExtractionItem)


# ----- registry ------------------------------------------------------------------ #


def test_known_modules_includes_finance() -> None:
    assert "finance" in known_modules()


def test_resolve_finance_builds_module() -> None:
    factory = resolve("finance")
    module = factory()
    assert isinstance(module, FinanceModule)


def test_resolve_unknown_raises() -> None:
    with pytest.raises(KeyError, match="no InterestModule registered"):
        resolve("does-not-exist")


# ----- disciplina de Protocol ---------------------------------------------------- #


def test_finance_satisfies_interest_module() -> None:
    """FinanceModule debe satisfacer estructuralmente el Protocol InterestModule."""
    assert isinstance(FinanceModule(), InterestModule)


def test_finance_declares_extract_capability() -> None:
    assert CAP_EXTRACT in FinanceModule.capabilities
