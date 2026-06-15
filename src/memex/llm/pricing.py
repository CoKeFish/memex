"""Tabla de precios + cálculo de costo por llamada (USD).

DeepSeek devuelve solo conteos de tokens (no el costo en USD, a diferencia de
Apify) → el costo se calcula acá desde una tabla por modelo, distinguiendo tokens
de prompt servidos desde cache (`cache_hit`, más baratos) de los no-cacheados
(`cache_miss`) y los de salida (`output`).

⚠ PRECIOS VOLÁTILES — verificados 2026-06-03 contra la doc oficial
(https://api-docs.deepseek.com/quick_start/pricing), USD por 1M de tokens:
  - deepseek-chat / deepseek-reasoner: identificadores DEPRECADOS (se retiran 2026-07-24).
        Hoy son ALIAS de deepseek-v4-flash (non-thinking / thinking) → se cobran a tarifa flash.
  - deepseek-v4-flash  cache_hit 0.0028 / cache_miss 0.14 / output 0.28
        (= deepseek-chat) — contexto 1M, salida 384K. OFICIAL.
  - deepseek-v4-pro    cache_hit 0.003625 / cache_miss 0.435 / output 0.87
        contexto 1M, salida 384K. OFICIAL.
  Histórico: V3.2-Exp valía 0.028 / 0.28 / 0.42; deepseek-chat ya NO mapea ahí (DeepSeek lo
  remapeó a v4-flash, ~10x más barato en hit, ~2x en miss).

Notas:
- Una promo -75% sobre v4-pro venció 2026-05-31; ya no se aplica.
- Off-peak histórico de V3.2 (UTC 16:30-00:30): ~50% (chat) / ~75% (reasoner). DeepSeek
  NO lo confirmó oficialmente para V3.2-Exp/V4, así que `off_peak_discount` queda en 0
  por default (aplicar un descuento NO confirmado SUBESTIMARÍA el costo real). El
  mecanismo y la ventana están listos; el usuario habilita los descuentos vía
  `MEMEX_LLM_PRICING` (campo `off_peak_discount` por modelo).
- Todo es override-able por entorno: `MEMEX_LLM_PRICING` (JSON por modelo) y
  `MEMEX_LLM_OFFPEAK_UTC` (ventana "HH:MM-HH:MM").

Query canónica de costo por source para agregación histórica (LEFT JOIN + etiqueta que
muestra las filas sin source de calendar como "(calendar)" para no perderlas de vista):

    SELECT COALESCE(
             s.name,
             CASE WHEN lc.purpose LIKE 'calendar%' THEN '(calendar)' ELSE '(sin source)' END
           ) AS source,
           COUNT(*) AS calls,
           SUM(lc.prompt_tokens + lc.completion_tokens) AS tokens,
           SUM(lc.cost_usd) AS cost
    FROM llm_calls lc
    LEFT JOIN sources s ON s.id = lc.source_id
    GROUP BY 1
    ORDER BY cost DESC;
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time
from decimal import Decimal

from memex.llm.client import LLMError, LLMUsage
from memex.logging import get_logger

_log = get_logger("memex.llm.pricing")

#: Modelos ya avisados como no-tabulados — un warning POR PROCESO por modelo, no por llamada
#: (un modelo sin tabular puede usarse cientos de veces por corrida).
_WARNED_UNKNOWN: set[str] = set()

#: Nombre de la env var con overrides de pricing (JSON por modelo).
_PRICING_ENV = "MEMEX_LLM_PRICING"
#: Nombre de la env var con la ventana off-peak en UTC ("HH:MM-HH:MM").
_OFFPEAK_ENV = "MEMEX_LLM_OFFPEAK_UTC"

_PER_MILLION = Decimal(1_000_000)
#: Precisión de la columna llm_calls.cost_usd (NUMERIC(10,6)).
_COST_QUANTUM = Decimal("0.000001")

#: Ventana off-peak default (UTC). Wrap de medianoche: start > end → cruza las 00:00.
_DEFAULT_OFFPEAK_START = time(16, 30)
_DEFAULT_OFFPEAK_END = time(0, 30)


class PricingConfigError(LLMError):
    """`MEMEX_LLM_PRICING`/`MEMEX_LLM_OFFPEAK_UTC` con JSON o shape inválido.

    Subclasea `LLMError` para que los callers atrapen la base genérica (igual que
    `LLMConfigError`). NO se falla silencioso: un override malformado debe romper
    explícito, no caer a defaults y enmascarar un error de configuración (no aflojar).
    """

    def __init__(self, message: str) -> None:
        super().__init__(0, message)


@dataclass(frozen=True)
class ModelPricing:
    """Precio de un modelo en USD por 1M de tokens.

    `off_peak_discount` es la fracción (0..1) que se descuenta del costo durante la
    ventana off-peak (0 = sin descuento, el default conservador).
    """

    cache_hit: Decimal
    cache_miss: Decimal
    output: Decimal
    off_peak_discount: Decimal = Decimal(0)


# Defaults verificados 2026-06-03 vs doc oficial de DeepSeek. `deepseek-chat`/`deepseek-reasoner`
# son alias DEPRECADOS (retiro 2026-07-24) de deepseek-v4-flash; `deepseek-v4-flash-preview` es la
# variante preview de v4-flash → todos se cobran a tarifa flash (sin estos alias el costo era $0).
_FLASH = ModelPricing(Decimal("0.0028"), Decimal("0.14"), Decimal("0.28"))
_PRO = ModelPricing(Decimal("0.003625"), Decimal("0.435"), Decimal("0.87"))

# Anthropic (verificado 2026-06-12): Opus 4.8 = $5 input / $25 output / $0.50 cache-read por 1M.
# La ESCRITURA de cache (cache_creation, premium 1.25x) se cobra acá a tarifa miss — subestima
# ~25% solo esos tokens; los callers actuales no usan cache_control, así que en la práctica son
# 0. Off-peak no aplica (es un descuento de DeepSeek): off_peak_discount=0 default.
_OPUS_4_8 = ModelPricing(Decimal("0.50"), Decimal("5.00"), Decimal("25.00"))

#: Tabla de precios pública por default (fallback si no hay overrides de entorno).
MODEL_PRICING: dict[str, ModelPricing] = {
    "deepseek-chat": _FLASH,
    "deepseek-reasoner": _FLASH,
    "deepseek-v4-flash": _FLASH,
    "deepseek-v4-flash-preview": _FLASH,
    "deepseek-v4-pro": _PRO,
    "claude-opus-4-8": _OPUS_4_8,
}


def load_pricing(env: Mapping[str, str] | None = None) -> dict[str, ModelPricing]:
    """Resuelve la tabla de pricing: defaults + overrides de `MEMEX_LLM_PRICING`.

    Parte de una copia de `MODEL_PRICING`. Si `MEMEX_LLM_PRICING` está seteada se
    parsea como JSON `{model: {"cache_hit":.., "cache_miss":.., "output":..,
    "off_peak_discount"?:..}}` y se superpone/extiende por modelo (con `Decimal(str(v))`).
    JSON inválido o shape inesperado → `PricingConfigError` (no se falla silencioso).
    Vacío/ausente → solo defaults.
    """
    env_map: Mapping[str, str] = env if env is not None else os.environ
    pricing: dict[str, ModelPricing] = dict(MODEL_PRICING)

    raw = env_map.get(_PRICING_ENV, "").strip()
    if not raw:
        return pricing

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PricingConfigError(f"{_PRICING_ENV} is not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise PricingConfigError(f"{_PRICING_ENV} must be a JSON object {{model: {{...}}}}")

    for model, spec in parsed.items():
        if not isinstance(spec, dict):
            raise PricingConfigError(f"{_PRICING_ENV}[{model!r}] must be a JSON object")
        try:
            pricing[model] = ModelPricing(
                cache_hit=Decimal(str(spec["cache_hit"])),
                cache_miss=Decimal(str(spec["cache_miss"])),
                output=Decimal(str(spec["output"])),
                off_peak_discount=Decimal(str(spec.get("off_peak_discount", 0))),
            )
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise PricingConfigError(
                f"{_PRICING_ENV}[{model!r}] has invalid pricing: {exc}"
            ) from exc

    return pricing


def _offpeak_window(env: Mapping[str, str] | None = None) -> tuple[time, time]:
    """Resuelve la ventana off-peak (UTC) desde `MEMEX_LLM_OFFPEAK_UTC` o el default."""
    env_map: Mapping[str, str] = env if env is not None else os.environ
    raw = env_map.get(_OFFPEAK_ENV, "").strip()
    if not raw:
        return _DEFAULT_OFFPEAK_START, _DEFAULT_OFFPEAK_END

    try:
        start_s, end_s = raw.split("-", 1)
        start = time.fromisoformat(start_s.strip())
        end = time.fromisoformat(end_s.strip())
    except ValueError as exc:
        raise PricingConfigError(f"{_OFFPEAK_ENV} must be 'HH:MM-HH:MM' (UTC): {exc}") from exc
    return start, end


def is_off_peak(at: datetime, *, env: Mapping[str, str] | None = None) -> bool:
    """¿`at` cae dentro de la ventana off-peak (UTC)?

    Maneja el wrap de medianoche (start > end → la ventana cruza las 00:00). `at` se
    interpreta en UTC: naive se asume UTC, aware se convierte.
    """
    moment = at.astimezone(UTC) if at.tzinfo is not None else at.replace(tzinfo=UTC)
    now = moment.timetz().replace(tzinfo=None)
    start, end = _offpeak_window(env)
    if start <= end:
        return start <= now < end
    # Wrap de medianoche: dentro si es después del start O antes del end.
    return now >= start or now < end


def compute_cost(
    model: str,
    usage: LLMUsage,
    *,
    pricing: Mapping[str, ModelPricing] | None = None,
    at: datetime | None = None,
) -> Decimal:
    """Costo USD de una llamada, cuantizado a 6 decimales.

    `pricing=None` usa la tabla global `MODEL_PRICING` (back-compat: el caller
    `compute_cost(model, usage)` sigue funcionando para los tests existentes). Si `at`
    no es None y cae en la ventana off-peak y el modelo tiene `off_peak_discount>0`, el
    costo se multiplica por `(1 - off_peak_discount)`.

    Modelo no tabulado → `Decimal(0)` + warning `llm.pricing.unknown_model` (una vez por
    proceso por modelo): no revienta el run, pero la tabla desactualizada deja rastro propio —
    confiar en que alguien note costos en $0 ya falló (H-4: gpt-4o-mini estuvo meses en $0).
    """
    table = pricing if pricing is not None else MODEL_PRICING
    rate = table.get(model)
    if rate is None:
        if model not in _WARNED_UNKNOWN:
            _WARNED_UNKNOWN.add(model)
            _log.warning("llm.pricing.unknown_model", model=model)
        return Decimal(0)

    cost = (
        rate.cache_hit * usage.cache_hit_tokens
        + rate.cache_miss * usage.cache_miss_tokens
        + rate.output * usage.completion_tokens
    ) / _PER_MILLION

    if at is not None and rate.off_peak_discount > 0 and is_off_peak(at):
        cost = cost * (Decimal(1) - rate.off_peak_discount)

    return cost.quantize(_COST_QUANTUM)
