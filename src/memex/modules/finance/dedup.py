"""Dedup determinista FASE 1 de finance (ADR-015 §4): función PURA, sin DB ni LLM.

Calca `calendar/dedup.py` pero con TRES bandas de decisión (calendar solo tiene candidato/no-par),
modelando lo que pidió el dueño: "dos montos iguales en la misma hora son probablemente el mismo
cobro; la probabilidad sube si más campos coinciden; si la probabilidad es lo bastante baja pero
cumple los requisitos, lo decide un LLM; si claramente es el mismo, la consolidación lo fusiona".

Compuerta mínima (si no, los movimientos coexisten, no son par): MISMO MONTO y proximidad temporal.
Mismo monto = igualdad EXACTA si la moneda coincide; si difiere (banco en pesos vs factura en
dólares) se convierte con `fx` y se compara en una BANDA de tolerancia (la conversión es aprox.).
La proximidad depende de la precisión del instante: si AMBOS lados tienen la hora del cobro
(`precision='datetime'`) se exige ≤ 1h ("misma hora"); si no (solo fecha, o fecha inferida de la
recepción), se compara a nivel DÍA (ventana ancha): la hora no es confiable.

Sobre la compuerta se suma un SCORE con los campos que coinciden (contraparte, lugar, rubro,
dirección). El score cae en tres bandas:
- `>= BAND_CONFIRM` Y ambos con hora (`datetime`) Y misma moneda → `confirmed` (procedimental, sin
  LLM): mismo monto + misma hora + contraparte/lugar fuertes = casi seguro el mismo cargo. Un par
  CROSS-CURRENCY nunca auto-confirma (conversión difusa): queda `candidate` para la FASE 2 LLM.
- `[BAND_CANDIDATE, ...)` (o score alto sin hora confiable, o cross-currency) → `candidate`: LLM.
- `< BAND_CANDIDATE` → sin par (coexisten).

`_same_responsible` es el SEAM de identidad (ADR-015): compara por `counterparty_identity_id` si
ambos lados resolvieron (misma id → mismo responsable; ids distintas → VETA el par); cae al texto
(`contract.normalize` + `difflib`) si no.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher

from memex.modules.contract import normalize
from memex.modules.finance import fx

#: Precisión del instante del cobro (espeja la columna `occurred_at_precision`).
PRECISION_DATETIME = "datetime"  # hora del cobro conocida → la compuerta exige "misma hora"
PRECISION_DATE = "date"  # solo la fecha; hora a medianoche (placeholder) → compuerta por día
PRECISION_INFERRED = "inferred"  # sin fecha en el mensaje; se usó la recepción → compuerta por día

#: Ventanas temporales de la compuerta.
DEFAULT_HOUR_WINDOW = timedelta(hours=1)  # ambos `datetime`: "misma hora"
DEFAULT_DAY_WINDOW = timedelta(hours=30)  # sin hora confiable: mismo día (+ holgura de recepción)

#: Umbral de similitud de texto (SequenceMatcher) para contar contraparte/lugar como "coinciden".
DEFAULT_TEXT_THRESHOLD = 0.85

#: Pesos del score aditivo (suman en [0,1]). La base depende de la precisión temporal: mismo monto +
#: misma HORA ya es "probablemente el mismo" (entra como candidato); mismo monto + mismo DÍA es más
#: débil y necesita corroboración para llegar a candidato.
W_GATE_HOUR = 0.40
W_GATE_DAY = 0.25
W_COUNTERPARTY = 0.35
W_PLACE = 0.15
W_CATEGORY = 0.05
W_DIRECTION = 0.05

#: Bandas de decisión sobre el score final.
DEFAULT_BAND_CONFIRM = 0.85  # >= y ambos `datetime` → confirmado procedimentalmente (sin LLM)
DEFAULT_BAND_CANDIDATE = 0.40  # [esto, confirm) → candidato (FASE 2 LLM); < esto → coexisten


@dataclass(frozen=True)
class DedupRow:
    """Una transacción a comparar. `occurred_at` es el mejor instante conocido; `precision` dice qué
    tan confiable es su hora (ver constantes `PRECISION_*`). `counterparty_identity_id` es la
    identidad del directorio a la que resolvió la contraparte (None si no resolvió o identidades
    está apagado): cuando ambos lados la tienen, el dedup compara responsables por IDENTIDAD, no
    por texto."""

    transaction_id: int
    direction: str
    amount: Decimal
    currency: str
    category: str
    counterparty: str
    place: str
    occurred_at: datetime
    precision: str
    counterparty_identity_id: int | None = None


@dataclass(frozen=True)
class DedupPair:
    """Par candidato canónico (`a_id < b_id`) con la razón, su score y la decisión."""

    a_id: int
    b_id: int
    reason: str  # resumen de señales, ej. 'amount+hora+contraparte+lugar'
    score: float
    decision: str  # 'confirmed' (procedimental) | 'candidate' (→ FASE 2 LLM)


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def _same_responsible(a: DedupRow, b: DedupRow, *, text_threshold: float) -> bool | None:
    """SEAM de identidad. Si AMBAS contrapartes resolvieron a una identidad del directorio
    (`counterparty_identity_id`), compara por IDENTIDAD: misma id → `True` (mismo responsable); ids
    DISTINTAS → `False` (responsables confirmadamente distintos → VETA el par en `_evaluate_pair`,
    aunque monto/fecha coincidan). Si al menos una no resolvió, cae al TEXTO: alta similitud →
    `True`; si no → `None` (el texto no puede AFIRMAR que son distintos — eso lo deja a la FASE 2
    LLM). El scoring solo suma el peso de contraparte cuando esto es `True`."""
    if a.counterparty_identity_id is not None and b.counterparty_identity_id is not None:
        return a.counterparty_identity_id == b.counterparty_identity_id
    if not a.counterparty.strip() or not b.counterparty.strip():
        return None
    if _similarity(a.counterparty, b.counterparty) >= text_threshold:
        return True
    return None


def _both_timed(a: DedupRow, b: DedupRow) -> bool:
    """Ambos con hora confiable (`datetime`) Y misma moneda: solo entonces se compara/puntúa a nivel
    HORA. Un par CROSS-CURRENCY viene de dos sistemas distintos (banco vs comercio) cuyos timestamps
    no alinean a la hora → se compara a nivel DÍA aunque ambos sean `datetime`."""
    same_currency = a.currency.strip().upper() == b.currency.strip().upper()
    return a.precision == PRECISION_DATETIME and b.precision == PRECISION_DATETIME and same_currency


def _gate(
    a: DedupRow,
    b: DedupRow,
    *,
    hour_window: timedelta,
    day_window: timedelta,
    fx_rates: Mapping[str, Decimal],
    fx_tolerance: Decimal,
) -> float | None:
    """¿El par pasa la compuerta mínima? Devuelve el peso BASE (según precisión temporal) o `None`.
    Requiere MISMO MONTO (exacto si igual moneda; convertido dentro de la banda `fx_tolerance` si
    difiere — sin tasa para alguna moneda no son par) y proximidad temporal (hora solo si
    `_both_timed`, si no día)."""
    if not fx.approx_equal(
        a.amount, a.currency, b.amount, b.currency, tol=fx_tolerance, rates=fx_rates
    ):
        return None
    both_timed = _both_timed(a, b)
    window = hour_window if both_timed else day_window
    if abs(a.occurred_at - b.occurred_at) > window:
        return None
    return W_GATE_HOUR if both_timed else W_GATE_DAY


def _evaluate_pair(
    a: DedupRow,
    b: DedupRow,
    *,
    hour_window: timedelta,
    day_window: timedelta,
    text_threshold: float,
    band_confirm: float,
    band_candidate: float,
    fx_rates: Mapping[str, Decimal],
    fx_tolerance: Decimal,
) -> DedupPair | None:
    base = _gate(
        a,
        b,
        hour_window=hour_window,
        day_window=day_window,
        fx_rates=fx_rates,
        fx_tolerance=fx_tolerance,
    )
    if base is None:
        return None

    resp = _same_responsible(a, b, text_threshold=text_threshold)
    if resp is False:  # seam de identidad: responsables distintos confirmados → veta el par
        return None

    cross_currency = a.currency.strip().upper() != b.currency.strip().upper()
    both_timed = _both_timed(a, b)  # ya excluye cross-currency (timestamps de sistemas distintos)
    signals = ["amount"]
    if cross_currency:
        signals.append("fx")  # monto equivalente por conversión, no idéntico
    signals.append("hora" if both_timed else "dia")

    score = base
    if resp is True:
        score += W_COUNTERPARTY
        signals.append("contraparte")
    if a.place.strip() and b.place.strip() and _similarity(a.place, b.place) >= text_threshold:
        score += W_PLACE
        signals.append("lugar")
    if a.category == b.category:
        score += W_CATEGORY
        signals.append("rubro")
    if a.direction == b.direction:
        score += W_DIRECTION
        signals.append("direccion")
    score = min(score, 1.0)

    if score < band_candidate:
        return None
    # Auto-confirmar (saltear el LLM) solo con `_both_timed` (hora confiable Y misma moneda): mismo
    # monto + MISMA HORA + señales fuertes ≈ el mismo cargo de dos fuentes. Sin hora confiable, o
    # cross-currency (conversión difusa), aunque el score sea alto lo decide el LLM.
    decision = "confirmed" if (score >= band_confirm and both_timed) else "candidate"

    lo, hi = (a, b) if a.transaction_id < b.transaction_id else (b, a)
    return DedupPair(
        a_id=lo.transaction_id,
        b_id=hi.transaction_id,
        reason="+".join(signals),
        score=round(score, 3),
        decision=decision,
    )


def mark_duplicates(
    new_rows: Sequence[DedupRow],
    existing_rows: Sequence[DedupRow],
    *,
    hour_window: timedelta = DEFAULT_HOUR_WINDOW,
    day_window: timedelta = DEFAULT_DAY_WINDOW,
    text_threshold: float = DEFAULT_TEXT_THRESHOLD,
    band_confirm: float = DEFAULT_BAND_CONFIRM,
    band_candidate: float = DEFAULT_BAND_CANDIDATE,
    fx_rates: Mapping[str, Decimal] | None = None,
    fx_tolerance: Decimal | None = None,
) -> list[DedupPair]:
    """Devuelve los pares candidatos de duplicado, comparando las transacciones NUEVAS entre sí y
    contra las EXISTENTES (nunca existentes contra existentes — ya se compararon en su corrida).

    Pura y determinista: pares canónicos (`a_id < b_id`), ordenados por `(a_id, b_id)`. Las tasas y
    la banda de conversión se resuelven UNA vez (defaults + env si no se pasan).
    """
    rates = fx_rates if fx_rates is not None else fx.load_rates()
    tolerance = fx_tolerance if fx_tolerance is not None else fx.load_tolerance()
    pairs: list[DedupPair] = []

    def evaluate(a: DedupRow, b: DedupRow) -> None:
        pair = _evaluate_pair(
            a,
            b,
            hour_window=hour_window,
            day_window=day_window,
            text_threshold=text_threshold,
            band_confirm=band_confirm,
            band_candidate=band_candidate,
            fx_rates=rates,
            fx_tolerance=tolerance,
        )
        if pair is not None:
            pairs.append(pair)

    for i in range(len(new_rows)):
        for j in range(i + 1, len(new_rows)):
            evaluate(new_rows[i], new_rows[j])
        for existing in existing_rows:
            evaluate(new_rows[i], existing)

    pairs.sort(key=lambda p: (p.a_id, p.b_id))
    return pairs
