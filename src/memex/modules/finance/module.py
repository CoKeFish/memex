"""`FinanceModule` — extractor puro de gastos. Satisface `InterestModule` estructuralmente.

Sin dependencias, sin dominio consolidador, sin servicios externos: el módulo más simple, que
valida el contrato completo (ADR-015 §11). `consumes_kinds` excluye SOCIAL a propósito — los
gastos viven en correos (banco/recibos) y chats; excluir social ejercita el pre-filtro.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar

from sqlalchemy import text

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, ModuleContext
from memex.modules.finance.prompt import FINANCE_SYSTEM_PROMPT
from memex.modules.finance.schema import ExpenseItem

_log = get_logger("memex.modules.finance")


class FinanceModule:
    """Extrae gastos a `mod_finance_expenses`."""

    slug: ClassVar[str] = "finance"
    interest: ClassVar[str] = (
        "Gastos y pagos de la persona: dinero que pagó o le cobraron — servicios (luz, agua, "
        "internet), compras, consumos de tarjeta, transferencias, restaurantes, transporte. "
        "NO publicidad ni promociones."
    )
    extraction_schema: ClassVar[type[ExtractionItem]] = ExpenseItem
    extraction_prompt: ClassVar[str] = FINANCE_SYSTEM_PROMPT
    capabilities: ClassVar[frozenset[str]] = frozenset({CAP_EXTRACT})
    consumes_kinds: ClassVar[frozenset[SourceKind]] = frozenset({SourceKind.EMAIL, SourceKind.CHAT})
    depends_on: ClassVar[tuple[str, ...]] = ()

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Inserta los gastos validados en `mod_finance_expenses` usando `ctx.conn`."""
        expenses = [i for i in items if isinstance(i, ExpenseItem)]
        if not expenses:
            return 0
        ctx.conn.execute(
            text(
                """
                INSERT INTO mod_finance_expenses
                  (user_id, source_inbox_ids, amount, currency, merchant,
                   occurred_on, description, evidence)
                VALUES
                  (:uid, :ids, :amount, :currency, :merchant,
                   :occurred_on, :description, :evidence)
                """
            ),
            [
                {
                    "uid": ctx.user_id,
                    "ids": list(e.source_inbox_ids),
                    "amount": e.amount,
                    "currency": e.currency,
                    "merchant": e.merchant,
                    "occurred_on": e.occurred_on,
                    "description": e.description,
                    "evidence": e.evidence,
                }
                for e in expenses
            ],
        )
        return len(expenses)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="finance module ready", checked_at=datetime.now(UTC)
        )
