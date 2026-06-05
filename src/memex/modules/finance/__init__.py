"""Módulo `finance` — extractor de TRANSACCIONES (ingresos/egresos) con dedup en dos fases +
consolidación (ADR-015 §11). Calca el patrón de calendar (crudas → FASE 1 procedimental → FASE 2
LLM → consolidación)."""

from __future__ import annotations

from memex.modules.finance.module import FinanceModule
from memex.modules.finance.schema import TransactionItem

__all__ = ["FinanceModule", "TransactionItem"]
