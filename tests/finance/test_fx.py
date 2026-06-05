"""Tests puros de `finance/fx.py`: conversión, banda de tolerancia y overrides por entorno."""

from __future__ import annotations

from decimal import Decimal

import pytest

from memex.modules.finance import fx


def test_convert_same_currency_is_identity() -> None:
    assert fx.convert(Decimal("100"), "USD", "USD") == Decimal("100")


def test_convert_known_pair() -> None:
    # 200000 COP a USD con la tasa default (0.00025 USD/COP) = 50 USD.
    assert fx.convert(Decimal("200000"), "COP", "USD") == Decimal("50.00000")


def test_convert_case_insensitive() -> None:
    assert fx.convert(Decimal("200000"), "cop", "usd") == Decimal("50.00000")


def test_convert_unknown_currency_is_none() -> None:
    assert fx.convert(Decimal("100"), "USD", "XYZ") is None
    assert fx.convert(Decimal("100"), "XYZ", "USD") is None


def test_approx_equal_same_currency_is_exact() -> None:
    assert fx.approx_equal(Decimal("100"), "USD", Decimal("100"), "USD") is True
    assert fx.approx_equal(Decimal("100"), "USD", Decimal("100.01"), "USD") is False


def test_approx_equal_cross_currency_within_band() -> None:
    # 50 USD vs 200000 COP (≈50 USD) → dentro de la banda.
    assert fx.approx_equal(Decimal("50"), "USD", Decimal("200000"), "COP") is True


def test_approx_equal_cross_currency_outside_band() -> None:
    # 50 USD vs 100000 COP (≈25 USD) → fuera de la banda del 12 %.
    assert fx.approx_equal(Decimal("50"), "USD", Decimal("100000"), "COP") is False


def test_approx_equal_unknown_currency_is_false() -> None:
    assert fx.approx_equal(Decimal("50"), "USD", Decimal("50"), "XYZ") is False


def test_approx_equal_tolerance_absorbs_rate_drift() -> None:
    # 50 USD vs 210000 COP (≈52.5 USD, +5 %): dentro de la banda del 12 %.
    assert fx.approx_equal(Decimal("50"), "USD", Decimal("210000"), "COP") is True


def test_load_rates_env_override() -> None:
    rates = fx.load_rates({"MEMEX_FX_RATES": '{"COP": "0.0005"}'})
    assert rates["COP"] == Decimal("0.0005")
    assert rates["USD"] == Decimal("1")  # default preservado


def test_load_rates_invalid_json_raises() -> None:
    with pytest.raises(fx.FxConfigError):
        fx.load_rates({"MEMEX_FX_RATES": "{not json"})


def test_load_rates_non_numeric_raises() -> None:
    with pytest.raises(fx.FxConfigError):
        fx.load_rates({"MEMEX_FX_RATES": '{"COP": "abc"}'})


def test_load_tolerance_env_override() -> None:
    assert fx.load_tolerance({"MEMEX_FX_TOLERANCE": "0.2"}) == Decimal("0.2")


def test_load_tolerance_out_of_range_raises() -> None:
    with pytest.raises(fx.FxConfigError):
        fx.load_tolerance({"MEMEX_FX_TOLERANCE": "1.5"})


def test_convert_with_custom_rates() -> None:
    rates = {"USD": Decimal("1"), "FOO": Decimal("2")}  # 1 FOO = 2 USD
    assert fx.convert(Decimal("10"), "FOO", "USD", rates=rates) == Decimal("20")
