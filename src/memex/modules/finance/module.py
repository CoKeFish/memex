"""`FinanceModule` — extractor puro de gastos. Satisface `InterestModule` estructuralmente.

Sin dependencias, sin dominio consolidador, sin servicios externos: el módulo más simple, que
valida el contrato completo (ADR-015 §11). `consumes_kinds` excluye SOCIAL a propósito — los
gastos viven en correos (banco/recibos) y chats; excluir social ejercita el pre-filtro.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import ClassVar

from memex.core.source import HealthResult, SourceKind
from memex.logging import get_logger
from memex.modules.contract import CAP_EXTRACT, ExtractionItem, ModuleContext
from memex.modules.dedup import upsert_unique
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
    #: business-key del vértice gasto. `merchant` se compara normalizado (lower + colapso de
    #: whitespace) por la DB; el UNIQUE de negocio (índice funcional) vive en la migración 0030
    #: (con `occurred_on` NULL = centinela).
    identity_fields: ClassVar[tuple[str, ...]] = ("currency", "amount", "merchant", "occurred_on")

    async def persist(self, ctx: ModuleContext, items: Sequence[ExtractionItem]) -> int:
        """Materializa cada gasto como VÉRTICE ÚNICO (dedup por business-key): si el mismo gasto ya
        existe (mismo monto/moneda/comercio-normalizado/fecha) fusiona `source_inbox_ids` (recibo +
        alerta del banco → un solo vértice con ambos documentos); si no, lo inserta. Atómico en
        `ctx.conn`. Devuelve cuántos gastos procesó."""
        expenses = [i for i in items if isinstance(i, ExpenseItem)]
        if not expenses:
            return 0
        for e in expenses:
            row = {
                "user_id": ctx.user_id,
                "source_inbox_ids": list(e.source_inbox_ids),
                "amount": e.amount,
                "currency": e.currency,
                "category": e.category,
                "merchant": e.merchant,
                "occurred_on": e.occurred_on,
                "description": e.description,
                "evidence": e.evidence,
            }
            identity = {
                "user_id": ctx.user_id,
                "currency": e.currency,
                "amount": e.amount,
                "merchant": e.merchant,
                "occurred_on": e.occurred_on,
            }
            upsert_unique(
                ctx.conn,
                "mod_finance_expenses",
                identity=identity,
                row=row,
                merge_arrays=("source_inbox_ids",),
                norm_text=("merchant",),
            )
        return len(expenses)

    async def health_check(self) -> HealthResult:
        return HealthResult(
            status="healthy", detail="finance module ready", checked_at=datetime.now(UTC)
        )
