"""GeoConfig.from_env — selección de proveedor, resolución de la key, redacción del secreto."""

from __future__ import annotations

import pytest

from memex.geo.client import GeoConfigError
from memex.geo.config import GeoConfig, known_providers


def test_from_env_google_default() -> None:
    cfg = GeoConfig.from_env({"GMAPS_API_KEY": "GKEY"})
    assert cfg.provider == "google"
    assert cfg.api_key.get_secret_value() == "GKEY"
    assert cfg.api_key_env == "GMAPS_API_KEY"
    assert cfg.base_url == "https://maps.googleapis.com"


def test_from_env_ors_via_arg() -> None:
    cfg = GeoConfig.from_env({"OPENROUTE_API_KEY": "OKEY"}, provider="ors")
    assert cfg.provider == "ors"
    assert cfg.api_key.get_secret_value() == "OKEY"
    assert cfg.base_url == "https://api.openrouteservice.org"


def test_provider_env_selects() -> None:
    cfg = GeoConfig.from_env({"MEMEX_GEO_PROVIDER": "ors", "OPENROUTE_API_KEY": "OKEY"})
    assert cfg.provider == "ors"


def test_arg_overrides_env_provider() -> None:
    cfg = GeoConfig.from_env(
        {"MEMEX_GEO_PROVIDER": "ors", "GMAPS_API_KEY": "GKEY"}, provider="google"
    )
    assert cfg.provider == "google"


def test_missing_key_raises() -> None:
    with pytest.raises(GeoConfigError):
        GeoConfig.from_env({}, provider="google")


def test_unknown_provider_raises() -> None:
    with pytest.raises(GeoConfigError):
        GeoConfig.from_env({"GMAPS_API_KEY": "x"}, provider="bing")


def test_base_url_override_arg() -> None:
    cfg = GeoConfig.from_env({"GMAPS_API_KEY": "x"}, base_url="http://proxy")
    assert cfg.base_url == "http://proxy"


def test_base_url_override_env() -> None:
    cfg = GeoConfig.from_env({"GMAPS_API_KEY": "x", "MEMEX_GEO_BASE_URL": "http://proxy"})
    assert cfg.base_url == "http://proxy"


def test_secret_redacted_in_repr_and_str() -> None:
    cfg = GeoConfig.from_env({"GMAPS_API_KEY": "SUPERSECRET"})
    assert "SUPERSECRET" not in repr(cfg)
    assert "SUPERSECRET" not in str(cfg)


def test_known_providers() -> None:
    assert known_providers() == ["google", "ors"]
