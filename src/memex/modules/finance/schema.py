"""`TransactionItem` — la forma de una transacción extraída (extraction_schema de finance v2).

Extiende `ExtractionItem` (atribución `source_inbox_ids` + `evidence`) con los campos de una
transacción (ingreso o egreso). `extra="forbid"`: un campo que el LLM invente fuera de este shape
invalida el item (se descarta + loguea) — mitigación de alucinación (ADR-015 §10).

Formato concreto (decisión del usuario):
- `direction` es OBLIGATORIO (ingreso/egreso); un valor faltante o raro cae a `egreso` (el caso
  común) en vez de descartar la transacción entera — mismo criterio que `category`→`otros` (con
  `extra=forbid`, un required que el LLM omite perdería el item).
- `status` (2026-09-07): ¿la plata se MOVIÓ? `completada` (default) | `fallida` (cobro
  rechazado/declinado/no procesado) | `pendiente` (por pagar: factura emitida, saldo, recordatorio).
  Pedirle al LLM que OMITA lo no concretado no fue confiable; clasificarlo sí. Solo `completada`
  cuenta en los totales; el resto se conserva con su estado (captura + perilla).
- `kind` (2026-09-07): `pago` (movimiento normal; default) | `reembolso` (plata que VUELVE de un
  cargo previo: devolución, nota crédito, reversión, chargeback). Un reembolso va como `ingreso`
  pero NO es un ingreso real: descuenta del gasto en su rubro.
- `amount` debe ser > 0: un «monto» 0 (factura en $0, recibo sin cifra, devolución sin monto) no es
  una transacción → el item se descarta (ValidationError → `extract.item.invalid`).
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

from memex.core.localtime import normalize_tz
from memex.modules.contract import ExtractionItem

#: Rubros válidos (espejo de `ExpenseCategory` en el frontend). El LLM debe elegir uno.
#: No son solo gastos: una transferencia a una persona, un traspaso entre cuentas propias,
#: un pago recibido o unos intereses tienen su rubro; «otros» es SOLO cuando no se sabe qué
#: es (decisión del dueño, 2026-09-16). La guía de cada uno vive en `rubros.CATEGORY_GUIDE`.
FINANCE_CATEGORIES: tuple[str, ...] = (
    "comida",
    "transporte",
    "software",
    "servicios",
    "educacion",
    "salud",
    "entretenimiento",
    "compras",
    "transferencia",
    "traspaso",
    "ingreso",
    "rendimientos",
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
    "compras",
    "transferencia",
    "traspaso",
    "ingreso",
    "rendimientos",
    "otros",
]

#: ingreso (entra plata) | egreso (sale plata). Cerrado; default `egreso`.
TransactionDirection = Literal["ingreso", "egreso"]

#: ¿Se movió la plata? Cerrado; default `completada`. Espejo del CHECK de la DB (0090).
TransactionStatus = Literal["completada", "fallida", "pendiente"]
FINANCE_STATUSES: tuple[str, ...] = ("completada", "fallida", "pendiente")
#: Estado que CUENTA en totales/KPIs: el único en que la plata efectivamente se movió.
STATUS_SETTLED = "completada"

#: pago (movimiento normal) | reembolso (plata que vuelve de un cargo previo). Default `pago`.
TransactionKind = Literal["pago", "reembolso"]
FINANCE_KINDS: tuple[str, ...] = ("pago", "reembolso")

_FAILED_WORDS = frozenset(
    {
        "fallida",
        "fallido",
        "failed",
        "unsuccessful",
        "rechazada",
        "rechazado",
        "rejected",
        "declinada",
        "declinado",
        "declined",
        "cancelada",
        "cancelado",
        "cancelled",
        "canceled",
        "no_procesada",
        "no_procesado",
    }
)
_PENDING_WORDS = frozenset(
    {"pendiente", "pending", "por_pagar", "unpaid", "due", "vencida", "vencido", "overdue"}
)
_REFUND_WORDS = frozenset(
    {
        "reembolso",
        "refund",
        "devolucion",
        "devolución",
        "nota_credito",
        "nota_crédito",
        "credit_note",
        "reversion",
        "reversión",
        "reversal",
        "chargeback",
        "contracargo",
    }
)


def normalize_status(v: object) -> str:
    """Sinónimos → `fallida` / `pendiente`; cualquier otra cosa (incl. faltante) → `completada`."""
    s = str(v or "").strip().lower().replace(" ", "_").replace("-", "_")
    if s in _FAILED_WORDS:
        return "fallida"
    if s in _PENDING_WORDS:
        return "pendiente"
    return "completada"


def normalize_kind(v: object) -> str:
    """Sinónimos → `reembolso`; cualquier otra cosa (incl. faltante) → `pago`."""
    s = str(v or "").strip().lower().replace(" ", "_").replace("-", "_")
    return "reembolso" if s in _REFUND_WORDS else "pago"


class TransactionItem(ExtractionItem):
    """Una transacción: dirección + monto + moneda (+ contraparte/lugar/fecha/rubro opcionales)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: TransactionDirection = "egreso"
    #: ¿La plata se movió? Solo `completada` cuenta; `fallida`/`pendiente` se guardan con estado.
    status: TransactionStatus = "completada"
    #: `reembolso` = plata que vuelve de un cargo previo (descuenta del gasto, no es ingreso real).
    kind: TransactionKind = "pago"
    amount: Decimal
    currency: str
    category: FinanceCategory = "otros"
    counterparty: str = ""
    place: str = ""
    occurred_on: date | None = None
    occurred_time: time | None = None
    #: Zona EXPLÍCITA de `occurred_time` SOLO si el mensaje la dice («10:00 UTC», «GMT-5»);
    #: normalizada (`UTC` | `±HH:MM` | IANA). None = hora local del usuario (`core.localtime`).
    occurred_tz: str | None = None
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

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, v: object) -> str:
        """failed/unsuccessful/rechazado → 'fallida'; pending/por pagar → 'pendiente'; el resto
        (incl. faltante) → 'completada'. No se descarta el item por un estado raro."""
        return normalize_status(v)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, v: object) -> str:
        """refund/devolución/nota crédito/reversal → 'reembolso'; el resto → 'pago'."""
        return normalize_kind(v)

    @field_validator("amount")
    @classmethod
    def _positive_amount(cls, v: Decimal) -> Decimal:
        """Sin monto no hay transacción: 0 o negativo invalida el item (se descarta + loguea)."""
        if v <= 0:
            raise ValueError("amount must be > 0")
        return v

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

    @field_validator("occurred_tz", mode="before")
    @classmethod
    def _normalize_tz(cls, v: object) -> str | None:
        """Zona no reconocible (abreviatura ambigua, basura) → None: se asume local, no se
        descarta la transacción."""
        return normalize_tz(v)
