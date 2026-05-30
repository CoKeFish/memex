"""`ExpenseItem` — la forma de un gasto extraído (extraction_schema de finance).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de un
gasto. `extra="forbid"`: un campo que el LLM invente fuera de este shape invalida el item
(se descarta + loguea) — mitigación de alucinación (ADR-015 §10). `currency` queda crudo
(sin normalizar) en este slice.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import ConfigDict

from memex.modules.contract import ExtractionItem


class ExpenseItem(ExtractionItem):
    """Un gasto: monto + moneda + comercio (+ fecha/descripcion opcionales)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: str
    merchant: str
    occurred_on: date | None = None
    description: str = ""
