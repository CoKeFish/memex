"""Conversión de moneda aproximada para el dedup de finance (cross-currency).

El mismo gasto puede llegar dos veces en MONEDAS distintas — la alerta del banco en la moneda local
(p. ej. COP) y la factura del comercio en otra (p. ej. USD). Para detectar que son el mismo
movimiento NO sirve el match exacto de monto: hay que convertir y comparar dentro de una BANDA de
tolerancia (la conversión es aproximada y las tasas se mueven; además los dos mensajes no llegan
a la vez). Esta banda es deliberadamente ancha: absorbe el drift de la tasa entre fechas, el spread
bancario y el redondeo. Por eso un par cross-currency NUNCA se auto-confirma procedimentalmente —
queda `candidate` y lo cierra la FASE 2 LLM (que recibe la conversión como pista).

⚠ TASAS VOLÁTILES Y APROXIMADAS — son valores de referencia, no de mercado en tiempo real. Se
expresan como `USD por 1 unidad` de la moneda (USD = 1). Todo override-able por entorno:
`MEMEX_FX_RATES` (JSON `{ "COP": "0.00025", ... }`) y `MEMEX_FX_TOLERANCE` (fracción 0..1).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

#: Env var con overrides de tasas (JSON `{moneda: USD_por_unidad}`).
_FX_ENV = "MEMEX_FX_RATES"
#: Env var con la banda de tolerancia relativa (fracción 0..1).
_TOL_ENV = "MEMEX_FX_TOLERANCE"

#: Banda relativa default: dos montos convertidos se consideran "el mismo" si difieren ≤ 12 %.
DEFAULT_FX_TOLERANCE = Decimal("0.12")

#: Tasas default (USD por 1 unidad de la moneda). Aproximadas; override por `MEMEX_FX_RATES`.
DEFAULT_RATES: dict[str, Decimal] = {
    "USD": Decimal("1"),
    "EUR": Decimal("1.08"),
    "COP": Decimal("0.00025"),  # ~4000 COP por USD
    "MXN": Decimal("0.058"),  # ~17 MXN por USD
    "ARS": Decimal("0.001"),  # muy volátil — override recomendado
    "BRL": Decimal("0.18"),  # ~5.5 BRL por USD
    "CLP": Decimal("0.0011"),  # ~900 CLP por USD
    "PEN": Decimal("0.27"),  # ~3.7 PEN por USD
}


class FxConfigError(ValueError):
    """`MEMEX_FX_RATES`/`MEMEX_FX_TOLERANCE` con JSON o shape inválido. No se falla silencioso: un
    override malformado rompe explícito en vez de caer a defaults y enmascarar el error."""


def load_rates(env: Mapping[str, str] | None = None) -> dict[str, Decimal]:
    """Resuelve la tabla de tasas: defaults + overrides de `MEMEX_FX_RATES` (JSON
    `{moneda: USD_por_unidad}`). Las claves se normalizan a MAYÚSCULAS. JSON inválido o valor no
    numérico → `FxConfigError`. Vacío/ausente → solo defaults."""
    env_map: Mapping[str, str] = env if env is not None else os.environ
    rates: dict[str, Decimal] = dict(DEFAULT_RATES)

    raw = env_map.get(_FX_ENV, "").strip()
    if not raw:
        return rates

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FxConfigError(f"{_FX_ENV} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise FxConfigError(f"{_FX_ENV} must be a JSON object {{currency: usd_per_unit}}")

    for ccy, value in parsed.items():
        try:
            rates[str(ccy).strip().upper()] = Decimal(str(value))
        except (InvalidOperation, TypeError) as exc:
            raise FxConfigError(f"{_FX_ENV}[{ccy!r}] is not a number: {exc}") from exc
    return rates


def load_tolerance(env: Mapping[str, str] | None = None) -> Decimal:
    """Banda de tolerancia relativa desde `MEMEX_FX_TOLERANCE` o el default. Fuera de [0,1) o no
    numérico → `FxConfigError`."""
    env_map: Mapping[str, str] = env if env is not None else os.environ
    raw = env_map.get(_TOL_ENV, "").strip()
    if not raw:
        return DEFAULT_FX_TOLERANCE
    try:
        tol = Decimal(raw)
    except InvalidOperation as exc:
        raise FxConfigError(f"{_TOL_ENV} is not a number: {exc}") from exc
    if not (Decimal(0) <= tol < Decimal(1)):
        raise FxConfigError(f"{_TOL_ENV} must be in [0, 1): {tol}")
    return tol


def convert(
    amount: Decimal,
    from_ccy: str,
    to_ccy: str,
    *,
    rates: Mapping[str, Decimal] | None = None,
) -> Decimal | None:
    """Convierte `amount` de `from_ccy` a `to_ccy` con la tabla (default global si `rates=None`).
    Misma moneda → el monto tal cual. Si alguna moneda no está tabulada → `None` (no se puede
    comparar; el caller hace coexistir las filas en vez de adivinar)."""
    src = from_ccy.strip().upper()
    dst = to_ccy.strip().upper()
    if src == dst:
        return amount
    table = rates if rates is not None else DEFAULT_RATES
    rate_src = table.get(src)
    rate_dst = table.get(dst)
    if rate_src is None or rate_dst is None or rate_dst == 0:
        return None
    return amount * rate_src / rate_dst


def approx_equal(
    amount_a: Decimal,
    ccy_a: str,
    amount_b: Decimal,
    ccy_b: str,
    *,
    tol: Decimal | None = None,
    rates: Mapping[str, Decimal] | None = None,
) -> bool:
    """¿Son `(amount_a, ccy_a)` y `(amount_b, ccy_b)` aprox. el mismo valor? Convierte B a la moneda
    de A y compara con banda RELATIVA (`tol`, default `DEFAULT_FX_TOLERANCE`). Misma moneda →
    igualdad EXACTA (no se afloja lo que no necesita conversión). Sin tasa para alguna moneda →
    `False` (no se puede afirmar igualdad)."""
    if ccy_a.strip().upper() == ccy_b.strip().upper():
        return amount_a == amount_b
    converted = convert(amount_b, ccy_b, ccy_a, rates=rates)
    if converted is None:
        return False
    tolerance = tol if tol is not None else DEFAULT_FX_TOLERANCE
    largest = max(abs(amount_a), abs(converted))
    if largest == 0:
        return True
    return abs(amount_a - converted) / largest <= tolerance
