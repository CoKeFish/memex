"""compute_cost + load_pricing + off-peak — math, overrides por env, ventana off-peak."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from memex.llm.client import LLMUsage
from memex.llm.pricing import (
    MODEL_PRICING,
    ModelPricing,
    PricingConfigError,
    compute_cost,
    is_off_peak,
    load_pricing,
)

# Defaults verificados 2026-05-31 (ver docstring de pricing.py). Anclados acá para que un
# cambio de tabla rompa el test a propósito (precios volátiles = único lugar a tocar).
_V32 = ModelPricing(Decimal("0.028"), Decimal("0.28"), Decimal("0.42"))
_FLASH = ModelPricing(Decimal("0.0028"), Decimal("0.14"), Decimal("0.28"))
_PRO = ModelPricing(Decimal("0.0145"), Decimal("1.74"), Decimal("3.48"))


def _usage(*, prompt: int = 0, completion: int = 0, hit: int = 0, miss: int = 0) -> LLMUsage:
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
    )


# --- compute_cost: math con defaults ----------------------------------------------- #


def test_default_model_full_million_each_bucket() -> None:
    # deepseek-chat = v4-flash: 1M hit + 1M miss + 1M output = 0.0028 + 0.14 + 0.28 = 0.4228
    u = _usage(prompt=2_000_000, completion=1_000_000, hit=1_000_000, miss=1_000_000)
    assert compute_cost("deepseek-chat", u) == Decimal("0.422800")


def test_cache_hit_cheaper_than_miss() -> None:
    hit_only = compute_cost("deepseek-chat", _usage(prompt=1_000_000, hit=1_000_000))
    miss_only = compute_cost("deepseek-chat", _usage(prompt=1_000_000, miss=1_000_000))
    assert hit_only == Decimal("0.002800")  # deepseek-chat = v4-flash
    assert miss_only == Decimal("0.140000")
    assert hit_only < miss_only


def test_pro_more_expensive_than_flash() -> None:
    u = _usage(prompt=1000, completion=1000, miss=1000)
    assert compute_cost("deepseek-v4-pro", u) > compute_cost("deepseek-v4-flash", u)


def test_chat_and_reasoner_share_flash_pricing() -> None:
    u = _usage(prompt=1000, completion=1000, miss=1000)
    chat = compute_cost("deepseek-chat", u)
    assert compute_cost("deepseek-reasoner", u) == chat


def test_unknown_model_returns_zero() -> None:
    u = _usage(prompt=1000, completion=1000, miss=1000)
    assert compute_cost("gpt-9000", u) == Decimal(0)


def test_cost_quantized_to_six_decimals() -> None:
    # (0.0028*60 + 0.14*40 + 0.28*20) / 1e6 = (0.168 + 5.6 + 5.6) / 1e6 = 11.368e-6 → 0.000011
    c = compute_cost("deepseek-v4-flash", _usage(prompt=100, completion=20, hit=60, miss=40))
    assert c == Decimal("0.000011")
    assert c.as_tuple().exponent == -6


def test_back_compat_no_kwargs_unchanged() -> None:
    # compute_cost(model, usage) sin pricing/at sigue funcionando contra MODEL_PRICING.
    u = _usage(prompt=1000, completion=1000, miss=1000)
    assert compute_cost("deepseek-chat", u) == compute_cost(
        "deepseek-chat", u, pricing=MODEL_PRICING
    )


# --- load_pricing ------------------------------------------------------------------ #


def test_load_pricing_empty_env_is_defaults() -> None:
    assert load_pricing({}) == MODEL_PRICING


def test_load_pricing_override_existing_model() -> None:
    env = {
        "MEMEX_LLM_PRICING": json.dumps(
            {"deepseek-chat": {"cache_hit": 0.1, "cache_miss": 0.2, "output": 0.3}}
        )
    }
    pricing = load_pricing(env)
    assert pricing["deepseek-chat"] == ModelPricing(Decimal("0.1"), Decimal("0.2"), Decimal("0.3"))
    # los demás quedan en default
    assert pricing["deepseek-v4-flash"] == _FLASH


def test_load_pricing_adds_new_model() -> None:
    env = {
        "MEMEX_LLM_PRICING": json.dumps(
            {"new-model-x": {"cache_hit": 1, "cache_miss": 2, "output": 3}}
        )
    }
    pricing = load_pricing(env)
    u = _usage(prompt=1_000_000, completion=1_000_000, miss=1_000_000)
    # 2 (miss) + 3 (output) = 5 USD por 1M+1M
    assert compute_cost("new-model-x", u, pricing=pricing) == Decimal("5.000000")


def test_load_pricing_override_with_off_peak_discount() -> None:
    env = {
        "MEMEX_LLM_PRICING": json.dumps(
            {
                "deepseek-chat": {
                    "cache_hit": 0.028,
                    "cache_miss": 0.28,
                    "output": 0.42,
                    "off_peak_discount": 0.5,
                }
            }
        )
    }
    pricing = load_pricing(env)
    assert pricing["deepseek-chat"].off_peak_discount == Decimal("0.5")


def test_load_pricing_malformed_json_raises() -> None:
    with pytest.raises(PricingConfigError):
        load_pricing({"MEMEX_LLM_PRICING": "{not json"})


def test_load_pricing_bad_shape_not_object_raises() -> None:
    with pytest.raises(PricingConfigError):
        load_pricing({"MEMEX_LLM_PRICING": json.dumps([1, 2, 3])})


def test_load_pricing_missing_field_raises() -> None:
    with pytest.raises(PricingConfigError):
        load_pricing({"MEMEX_LLM_PRICING": json.dumps({"m": {"cache_hit": 1}})})


def test_load_pricing_model_spec_not_object_raises() -> None:
    with pytest.raises(PricingConfigError):
        load_pricing({"MEMEX_LLM_PRICING": json.dumps({"m": "cheap"})})


# --- off-peak ---------------------------------------------------------------------- #


def test_is_off_peak_default_window_midnight_wrap() -> None:
    # Ventana default UTC 16:30-00:30 (cruza medianoche).
    assert is_off_peak(datetime(2026, 5, 31, 18, 0, tzinfo=UTC)) is True
    assert is_off_peak(datetime(2026, 5, 31, 0, 15, tzinfo=UTC)) is True  # antes del end
    assert is_off_peak(datetime(2026, 5, 31, 16, 29, tzinfo=UTC)) is False
    assert is_off_peak(datetime(2026, 5, 31, 12, 0, tzinfo=UTC)) is False
    assert is_off_peak(datetime(2026, 5, 31, 0, 30, tzinfo=UTC)) is False  # end exclusivo


def test_is_off_peak_custom_non_wrapping_window() -> None:
    env = {"MEMEX_LLM_OFFPEAK_UTC": "09:00-17:00"}
    assert is_off_peak(datetime(2026, 5, 31, 12, 0, tzinfo=UTC), env=env) is True
    assert is_off_peak(datetime(2026, 5, 31, 8, 0, tzinfo=UTC), env=env) is False
    assert is_off_peak(datetime(2026, 5, 31, 17, 0, tzinfo=UTC), env=env) is False


def test_is_off_peak_naive_assumed_utc() -> None:
    assert is_off_peak(datetime(2026, 5, 31, 18, 0)) is True


def test_compute_cost_off_peak_applies_discount() -> None:
    pricing = {
        "m": ModelPricing(
            Decimal("0"), Decimal("1.0"), Decimal("0"), off_peak_discount=Decimal("0.5")
        )
    }
    u = _usage(prompt=1_000_000, miss=1_000_000)
    in_window = datetime(2026, 5, 31, 18, 0, tzinfo=UTC)
    out_window = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    assert compute_cost("m", u, pricing=pricing, at=in_window) == Decimal("0.500000")
    assert compute_cost("m", u, pricing=pricing, at=out_window) == Decimal("1.000000")


def test_compute_cost_off_peak_zero_discount_no_change() -> None:
    # Default off_peak_discount=0 → el costo no cambia ni dentro de la ventana.
    u = _usage(prompt=1000, completion=1000, miss=1000)
    in_window = datetime(2026, 5, 31, 18, 0, tzinfo=UTC)
    assert compute_cost("deepseek-chat", u, at=in_window) == compute_cost("deepseek-chat", u)
