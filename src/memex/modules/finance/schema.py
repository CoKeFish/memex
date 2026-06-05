"""`TransactionItem` — la forma de una transacción extraída (extraction_schema de finance v2).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de una
transacción (ingreso o egreso). `extra="forbid"`: un campo que el LLM invente fuera de este shape
invalida el item (se descarta + loguea) — mitigación de alucinación (ADR-015 §10).

Formato concreto (decisión del usuario):
- `direction` es OBLIGATORIO (ingreso/egreso); un valor faltante o raro cae a `egreso` (el caso
  común) en vez de descartar la transacción entera — mismo criterio que `category`→`otros` (con
  `extra=forbid`, un required que el LLM omite perdería el item).
- la FECHA del cobro se modela partida como en calendar (`occurred_on` DATE + `occurred_time` TIME),
  para no inventar timezone ni falsa precisión: si el mensaje no trae fecha, el MÓDULO infiere la de
  recepción al persistir (acá `occurred_on=None`). `occurred_time` solo si la hora aparece.
- `currency` se normaliza a mayúsculas (el prompt pide ISO 4217); `category` se elige de una lista
  cerrada de rubros.
- `counterparty` (quién cobró/pagó) es el seam de identidad; `place` (lugar físico o URL) va aparte.
"""

from __future__ import annotations

from datetime import date, time
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

#: ingreso (entra plata) | egreso (sale plata). Cerrado; default `egreso`.
TransactionDirection = Literal["ingreso", "egreso"]


class TransactionItem(ExtractionItem):
    """Una transacción: dirección + monto + moneda (+ contraparte/lugar/fecha/rubro opcionales)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: TransactionDirection = "egreso"
    amount: Decimal
    currency: str
    category: FinanceCategory = "otros"
    counterparty: str = ""
    place: str = ""
    occurred_on: date | None = None
    occurred_time: time | None = None
    description: str = ""

    @field_validator("direction", mode="before")
    @classmethod
    def _normalize_direction(cls, v: object) -> str:
        """ingreso/income/credit/abono/depósito → 'ingreso'; cualquier otra cosa (incl. faltante o
        None) → 'egreso' (el caso común): no se descarta la transacción por una dirección rara."""
        s = str(v or "").strip().lower()
        if s in {"ingreso", "income", "credit", "abono", "deposito", "depósito"}:
            return "ingreso"
        return "egreso"

    @field_validator("category", mode="before")
    @classmethod
    def _normalize_category(cls, v: object) -> str:
        """Rubro fuera de la lista → 'otros' (no descarta la transacción por un rubro inválido)."""
        s = str(v or "").strip().lower()
        return s if s in _CATEGORY_SET else "otros"

    @field_validator("currency", mode="before")
    @classmethod
    def _normalize_currency(cls, v: object) -> str:
        """A mayúsculas sin espacios (el prompt pide ISO 4217; los símbolos quedan como vienen)."""
        return str(v or "").strip().upper()
