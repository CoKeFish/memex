from __future__ import annotations

from pathlib import Path

import pytest

from memex_local_client import paths
from memex_local_client.config import LocalConfig, LocalConfigError


def test_home_dir_uses_env_override(local_home: Path) -> None:
    assert paths.home_dir() == local_home


def test_ensure_layout_creates_dirs(local_home: Path) -> None:
    paths.ensure_layout()
    assert (local_home / "plugins").is_dir()
    assert (local_home / "secrets").is_dir()


def test_config_loads_from_toml(local_home: Path) -> None:
    paths.ensure_layout()
    (local_home / "config.toml").write_text(
        'gateway_url = "http://memex.example/"\napi_token = "tok-1"\n',
        encoding="utf-8",
    )
    cfg = LocalConfig.load()
    assert cfg.gateway_url == "http://memex.example/"
    assert cfg.api_token == "tok-1"


def test_env_overrides_file(local_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths.ensure_layout()
    (local_home / "config.toml").write_text(
        'gateway_url = "http://from-file/"\napi_token = "tok-file"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MEMEX_LOCAL_GATEWAY_URL", "http://from-env/")
    monkeypatch.setenv("MEMEX_LOCAL_TOKEN", "tok-env")
    cfg = LocalConfig.load()
    assert cfg.gateway_url == "http://from-env/"
    assert cfg.api_token == "tok-env"


def test_missing_gateway_url_raises(local_home: Path) -> None:
    paths.ensure_layout()
    with pytest.raises(LocalConfigError):
        LocalConfig.load()
