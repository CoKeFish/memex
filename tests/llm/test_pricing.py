"""compute_cost — math de cache hit/miss/output, alias legacy, modelo desconocido."""

from __future__ import annotations

from decimal import Decimal

from memex.llm.client import LLMUsage
from memex.llm.pricing import compute_cost


def _usage(*, prompt: int = 0, completion: int = 0, hit: int = 0, miss: int = 0) -> LLMUsage:
    return LLMUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
    )


def test_flash_full_million_each_bucket() -> None:
    # 1M hit + 1M miss + 1M output, flash = 0.14 + 0.28 + 0.28 = 0.70
    u = _usage(prompt=2_000_000, completion=1_000_000, hit=1_000_000, miss=1_000_000)
    assert compute_cost("deepseek-v4-flash", u) == Decimal("0.700000")


def test_cache_hit_cheaper_than_miss() -> None:
    hit_only = compute_cost("deepseek-v4-flash", _usage(prompt=1_000_000, hit=1_000_000))
    miss_only = compute_cost("deepseek-v4-flash", _usage(prompt=1_000_000, miss=1_000_000))
    assert hit_only == Decimal("0.140000")
    assert miss_only == Decimal("0.280000")
    assert hit_only < miss_only


def test_pro_more_expensive_than_flash() -> None:
    u = _usage(prompt=1000, completion=1000, miss=1000)
    assert compute_cost("deepseek-v4-pro", u) > compute_cost("deepseek-v4-flash", u)


def test_legacy_aliases_priced_as_flash() -> None:
    u = _usage(prompt=1000, completion=1000, miss=1000)
    flash = compute_cost("deepseek-v4-flash", u)
    assert compute_cost("deepseek-chat", u) == flash
    assert compute_cost("deepseek-reasoner", u) == flash


def test_unknown_model_returns_zero() -> None:
    u = _usage(prompt=1000, completion=1000, miss=1000)
    assert compute_cost("gpt-9000", u) == Decimal(0)


def test_cost_quantized_to_six_decimals() -> None:
    # (0.14*60 + 0.28*40 + 0.28*20) / 1e6 = 25.2 / 1e6 = 0.0000252 → 0.000025
    c = compute_cost("deepseek-v4-flash", _usage(prompt=100, completion=20, hit=60, miss=40))
    assert c == Decimal("0.000025")
    assert c.as_tuple().exponent == -6
