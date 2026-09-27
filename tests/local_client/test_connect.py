from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from memex_local_client import paths
from memex_local_client.cli import _build_parser
from memex_local_client.config import LocalConfig
from memex_local_client.connect import (
    ConnectError,
    bundled_plugins_dir,
    check_connection,
    connect,
)

BASE = "http://localhost:8787"


def _me(*, auth_enforced: bool = False) -> dict[str, object]:
    return {
        "user_id": 1,
        "email": "me@local",
        "display_name": "default",
        "auth_enforced": auth_enforced,
    }


def test_check_connection_ok(local_home: Path) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get("/auth/me").respond(json=_me())
        who = check_connection(BASE)
    assert who.user_id == 1
    assert who.email == "me@local"
    assert who.auth_enforced is False


def test_connect_writes_config_stripping_trailing_slash(local_home: Path) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get("/auth/me").respond(json=_me())
        who = connect(BASE + "/")
    assert who.user_id == 1
    cfg = LocalConfig.load()
    assert cfg.gateway_url == BASE  # sin barra final
    assert cfg.api_token == ""
    assert (local_home / "config.toml").exists()


def test_connect_with_token_sends_bearer(local_home: Path) -> None:
    captured: dict[str, str | None] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_me(auth_enforced=True))

    with respx.mock(base_url=BASE) as router:
        router.get("/auth/me").mock(side_effect=responder)
        who = connect(BASE, "tok-123")
    assert captured["auth"] == "Bearer tok-123"
    assert who.auth_enforced is True
    assert LocalConfig.load().api_token == "tok-123"


def test_check_connection_rejects_bad_token(local_home: Path) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get("/auth/me").respond(403)
        with pytest.raises(ConnectError) as ei:
            check_connection(BASE, "bad")
    assert "token" in str(ei.value).lower()


def test_check_connection_unreachable(local_home: Path) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get("/auth/me").mock(side_effect=httpx.ConnectError("nope"))
        with pytest.raises(ConnectError) as ei:
            check_connection(BASE)
    assert "contactar" in str(ei.value).lower()


def test_connect_does_not_write_config_on_failure(local_home: Path) -> None:
    with respx.mock(base_url=BASE) as router:
        router.get("/auth/me").respond(403)
        with pytest.raises(ConnectError):
            connect(BASE, "bad")
    assert not (local_home / "config.toml").exists()


def test_config_save_roundtrip(local_home: Path) -> None:
    paths.ensure_layout()
    LocalConfig(gateway_url="http://x:8787", api_token="t").save()
    cfg = LocalConfig.load()
    assert cfg.gateway_url == "http://x:8787"
    assert cfg.api_token == "t"


def test_bundled_plugins_dir_has_selftest() -> None:
    d = bundled_plugins_dir()
    assert d is not None
    assert (d / "selftest" / "__init__.py").exists()


def test_parser_accepts_new_commands() -> None:
    p = _build_parser()
    for argv in (
        ["connect", "http://x"],
        ["setup"],
        ["doctor"],
        ["autostart", "enable"],
        ["autostart", "status"],
    ):
        ns = p.parse_args(argv)
        assert ns.group == argv[0]
