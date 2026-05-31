"""`ExpenseItem` — la forma de un gasto extraído (extraction_schema de finance).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de un
gasto. `extra="forbid"`: un campo que el LLM invente fuera de este shape invalida el item
(se descarta + loguea) — mitigación de alucinación (ADR-015 §10).

Formato concreto (decisión del usuario): `category` se elige de una **lista cerrada** de rubros
(default `otros` si el LLM omite o devuelve algo fuera de la lista, así no se descarta el gasto);
`currency` se normaliza a mayúsculas (el prompt pide código ISO 4217).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import ConfigDict, field_validator

from memex.modules.contract import ExtractionItem

#: Rubros válidos (espejo de `ExpenseCategory` en el frontend). El LLM debe elegir uno.
FINANCE_CATEGORIES: tuple[str, ...] = (
    "comida",
    "transporte",
    "software",
    "servicios",
    "educacion",
    "salud",
    "entretenimiento",
    "otros",
)
_CATEGORY_SET = frozenset(FINANCE_CATEGORIES)

FinanceCategory = Literal[
    "comida",
    "transporte",
    "software",
    "servicios",
    "educacion",
    "salud",
    "entretenimiento",
    "otros",
]


class ExpenseItem(ExtractionItem):
    """Un gasto: monto + moneda + rubro + comercio (+ fecha/descripción opcionales)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount: Decimal
    currency: str
    category: FinanceCategory = "otros"
    merchant: str
    occurred_on: date | None = None
    description: str = ""

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, v: object) -> str:
        """Rubro fuera de la lista → 'otros' (no descarta el gasto por una categoría inválida)."""
        s = str(v or "").strip().lower()
        return s if s in _CATEGORY_SET else "otros"

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, v: object) -> str:
        """A mayúsculas sin espacios (el prompt pide ISO 4217; los símbolos quedan como vienen)."""
        return str(v or "").strip().upper()
