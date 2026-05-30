"""Módulo `finance` — extractor puro de gastos (ADR-015 §11, primer módulo)."""

from __future__ import annotations

from memex.modules.finance.module import FinanceModule
from memex.modules.finance.schema import ExpenseItem

__all__ = ["ExpenseItem", "FinanceModule"]
