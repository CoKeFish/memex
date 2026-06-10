"""SocialConfig.from_source_config — env resolution, normalization, validation."""

from __future__ import annotations

from datetime import date

import pytest

from memex.core.source import SourceConfigError
from memex.ingestors.social.config import SocialConfig, SocialConfigError

VALID_ENV = {"MEMEX_APIFY_TOKEN": "apify_api_secret"}


def test_minimal_config_resolves_defaults_per_platform() -> None:
    cfg = SocialConfig.from_source_config({}, env=VALID_ENV, platform="instagram")
    assert cfg.platform == "instagram"
    assert cfg.apify_token.get_secret_value() == "apify_api_secret"
    assert cfg.actor_id == "apify/instagram-scraper"
    assert cfg.accounts == []
    assert cfg.results_limit == 30
    assert cfg.run_timeout_s == 120
    assert cfg.apify_token_env == "MEMEX_APIFY_TOKEN"


def test_default_actor_per_platform() -> None:
    fb = SocialConfig.from_source_config({}, env=VALID_ENV, platform="facebook")
    x = SocialConfig.from_source_config({}, env=VALID_ENV, platform="x")
    assert fb.actor_id == "apify/facebook-posts-scraper"
    assert x.actor_id == "apidojo/tweet-scraper"


def test_actor_id_override() -> None:
    cfg = SocialConfig.from_source_config(
        {"actor_id": "someone/custom-scraper"}, env=VALID_ENV, platform="x"
    )
    assert cfg.actor_id == "someone/custom-scraper"


def test_accounts_parsed_and_normalized() -> None:
    cfg = SocialConfig.from_source_config(
        {
            "accounts": [
                {"account": "@UTN.FRBA"},
                {"account": "https://www.instagram.com/Fiuba/", "priority": True},
                {"account": "https://x.com/utnfrba?lang=es"},
            ]
        },
        env=VALID_ENV,
        platform="instagram",
    )
    assert [a.account for a in cfg.accounts] == ["utn.frba", "fiuba", "utnfrba"]
    assert cfg.accounts[1].priority is True


def test_custom_token_env() -> None:
    cfg = SocialConfig.from_source_config(
        {"apify_token_env": "MY_TOKEN"},
        env={"MY_TOKEN": "xyz"},
        platform="facebook",
    )
    assert cfg.apify_token.get_secret_value() == "xyz"
    assert cfg.apify_token_env == "MY_TOKEN"


def test_missing_env_var_raises() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({}, env={}, platform="instagram")


def test_empty_env_var_raises() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({}, env={"MEMEX_APIFY_TOKEN": "   "}, platform="instagram")


def test_invalid_accounts_shape_raises() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({"accounts": "nope"}, env=VALID_ENV, platform="x")
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config(
            {"accounts": [{"account": "a", "bogus": 1}]}, env=VALID_ENV, platform="x"
        )


def test_non_positive_results_limit_raises() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({"results_limit": 0}, env=VALID_ENV, platform="x")


def test_non_positive_run_timeout_raises() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({"run_timeout_s": -1}, env=VALID_ENV, platform="x")


def test_config_error_is_source_config_error() -> None:
    """Generic except SourceConfigError catches social-specific errors."""
    assert issubclass(SocialConfigError, SourceConfigError)


def test_token_never_leaks_via_repr_str_or_dump() -> None:
    """SecretStr + __repr__ → el token no aparece en repr(), str(), f-string ni en
    model_dump/json. Solo se accede vía get_secret_value()."""
    cfg = SocialConfig.from_source_config({}, env=VALID_ENV, platform="instagram")
    assert "apify_api_secret" not in repr(cfg)
    assert "<redacted>" in repr(cfg)
    assert "apify_api_secret" not in str(cfg)
    assert "apify_api_secret" not in f"{cfg}"
    assert "apify_api_secret" not in cfg.model_dump_json()
    # El valor real solo se obtiene explícitamente.
    assert cfg.apify_token.get_secret_value() == "apify_api_secret"


# ---- Ventana de fetch (modos) + native_since + tope de gasto ------------------------------------


def test_fetch_window_defaults() -> None:
    cfg = SocialConfig.from_source_config({}, env=VALID_ENV, platform="instagram")
    assert cfg.fetch_mode == "incremental"
    assert cfg.fetch_since is None
    assert cfg.fetch_until is None
    assert cfg.fetch_limit is None
    assert cfg.native_since is True
    assert cfg.max_run_charge_usd is None


def test_fetch_window_parses_transient_keys() -> None:
    cfg = SocialConfig.from_source_config(
        {
            "fetch_mode": "range",
            "fetch_since": "2026-01-05",
            "fetch_until": "2026-02-01",
            "fetch_limit": 50,
            "native_since": False,
            "max_run_charge_usd": 1.5,
        },
        env=VALID_ENV,
        platform="x",
    )
    assert cfg.fetch_mode == "range"
    assert cfg.fetch_since == date(2026, 1, 5)
    assert cfg.fetch_until == date(2026, 2, 1)
    assert cfg.fetch_limit == 50
    assert cfg.native_since is False
    assert cfg.max_run_charge_usd == 1.5


def test_fetch_mode_invalid_raises() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({"fetch_mode": "bogus"}, env=VALID_ENV, platform="x")


def test_fetch_since_invalid_date_raises() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config(
            {"fetch_mode": "range", "fetch_since": "05/01/2026"}, env=VALID_ENV, platform="x"
        )


def test_fetch_limit_must_be_positive() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({"fetch_limit": 0}, env=VALID_ENV, platform="x")


def test_max_run_charge_must_be_positive() -> None:
    with pytest.raises(SocialConfigError):
        SocialConfig.from_source_config({"max_run_charge_usd": -1}, env=VALID_ENV, platform="x")
