from __future__ import annotations

import pytest

from memex.ingestors.imap.config import ImapConfig, ImapConfigError

VALID_CFG = {
    "server": "imap.example.com",
    "port": 993,
    "username_env": "TEST_IMAP_USER",
    "password_env": "TEST_IMAP_PASS",
    "folders": ["INBOX"],
}
VALID_ENV = {"TEST_IMAP_USER": "alice@example.com", "TEST_IMAP_PASS": "secret"}


def test_from_source_config_happy_path() -> None:
    cfg = ImapConfig.from_source_config(VALID_CFG, env=VALID_ENV)
    assert cfg.server == "imap.example.com"
    assert cfg.port == 993
    assert cfg.username == "alice@example.com"
    assert cfg.password == "secret"
    assert cfg.folders == ["INBOX"]
    # Defaults applied
    assert cfg.since_days == 7
    assert cfg.batch_size == 50
    assert cfg.fetch_body is True
    assert cfg.max_body_bytes == 524288
    assert cfg.use_ssl is True


def test_from_source_config_applies_overrides() -> None:
    cfg = ImapConfig.from_source_config(
        {**VALID_CFG, "since_days": 30, "batch_size": 100, "fetch_body": False},
        env=VALID_ENV,
    )
    assert cfg.since_days == 30
    assert cfg.batch_size == 100
    assert cfg.fetch_body is False


def test_password_not_in_repr() -> None:
    # Use a distinctive password string so substring matching doesn't collide
    # with field names like `oauth_client_secret_path`.
    env = {"TEST_IMAP_USER": "alice@example.com", "TEST_IMAP_PASS": "p4ssw0rd-VALUE"}
    cfg = ImapConfig.from_source_config(VALID_CFG, env=env)
    rendered = repr(cfg)
    assert "p4ssw0rd-VALUE" not in rendered
    assert "<redacted>" in rendered


def test_missing_server_raises() -> None:
    cfg = {k: v for k, v in VALID_CFG.items() if k != "server"}
    with pytest.raises(ImapConfigError, match="missing 'server'"):
        ImapConfig.from_source_config(cfg, env=VALID_ENV)


def test_empty_server_raises() -> None:
    with pytest.raises(ImapConfigError, match="'server' is empty"):
        ImapConfig.from_source_config({**VALID_CFG, "server": "  "}, env=VALID_ENV)


def test_missing_username_env_raises() -> None:
    cfg = {k: v for k, v in VALID_CFG.items() if k != "username_env"}
    with pytest.raises(ImapConfigError, match="username_env"):
        ImapConfig.from_source_config(cfg, env=VALID_ENV)


def test_env_var_not_set_raises() -> None:
    with pytest.raises(ImapConfigError, match="'TEST_IMAP_USER' is not set"):
        ImapConfig.from_source_config(VALID_CFG, env={})


def test_env_var_empty_raises() -> None:
    with pytest.raises(ImapConfigError, match="resolves to empty"):
        ImapConfig.from_source_config(
            VALID_CFG, env={"TEST_IMAP_USER": "", "TEST_IMAP_PASS": "secret"}
        )


def test_empty_folders_raises() -> None:
    with pytest.raises(ImapConfigError, match="folders"):
        ImapConfig.from_source_config({**VALID_CFG, "folders": []}, env=VALID_ENV)


def test_non_list_folders_raises() -> None:
    with pytest.raises(ImapConfigError, match="folders"):
        ImapConfig.from_source_config({**VALID_CFG, "folders": "INBOX"}, env=VALID_ENV)


def test_multiple_folders_preserved() -> None:
    cfg = ImapConfig.from_source_config(
        {**VALID_CFG, "folders": ["INBOX", "Sent", "Archive/2026"]},
        env=VALID_ENV,
    )
    assert cfg.folders == ["INBOX", "Sent", "Archive/2026"]


def test_default_auth_method_is_basic() -> None:
    """Backward compat: configs without 'auth' default to 'basic'."""
    cfg = ImapConfig.from_source_config(VALID_CFG, env=VALID_ENV)
    assert cfg.auth_method == "basic"


def test_explicit_basic_auth_works() -> None:
    cfg = ImapConfig.from_source_config({**VALID_CFG, "auth": "basic"}, env=VALID_ENV)
    assert cfg.auth_method == "basic"
    assert cfg.password == "secret"


def test_invalid_auth_value_raises() -> None:
    with pytest.raises(ImapConfigError, match=r"auth.*basic.*oauth2"):
        ImapConfig.from_source_config({**VALID_CFG, "auth": "kerberos"}, env=VALID_ENV)


# ----- OAuth2 -------------------------------------------------------------- #

VALID_OAUTH_CFG = {
    "server": "imap.gmail.com",
    "port": 993,
    "auth": "oauth2",
    "oauth_provider": "google",
    "username_env": "TEST_IMAP_USER",
    "oauth_client_secret_path_env": "TEST_OAUTH_CLIENT_SECRET",
    "oauth_token_path_env": "TEST_OAUTH_TOKEN",
    "folders": ["INBOX"],
}
VALID_OAUTH_ENV = {
    "TEST_IMAP_USER": "alice@gmail.com",
    "TEST_OAUTH_CLIENT_SECRET": "/path/to/client_secret.json",
    "TEST_OAUTH_TOKEN": "/path/to/token.json",
}


def test_oauth_happy_path() -> None:
    cfg = ImapConfig.from_source_config(VALID_OAUTH_CFG, env=VALID_OAUTH_ENV)
    assert cfg.auth_method == "oauth2"
    assert cfg.oauth_provider == "google"
    assert cfg.username == "alice@gmail.com"
    assert cfg.password == ""  # no password in oauth mode
    assert cfg.oauth_client_secret_path == "/path/to/client_secret.json"
    assert cfg.oauth_token_path == "/path/to/token.json"


def test_oauth_missing_client_secret_path_env_raises() -> None:
    cfg = {k: v for k, v in VALID_OAUTH_CFG.items() if k != "oauth_client_secret_path_env"}
    with pytest.raises(ImapConfigError, match="oauth_client_secret_path_env"):
        ImapConfig.from_source_config(cfg, env=VALID_OAUTH_ENV)


def test_oauth_missing_token_path_env_raises() -> None:
    cfg = {k: v for k, v in VALID_OAUTH_CFG.items() if k != "oauth_token_path_env"}
    with pytest.raises(ImapConfigError, match="oauth_token_path_env"):
        ImapConfig.from_source_config(cfg, env=VALID_OAUTH_ENV)


def test_oauth_client_secret_env_var_not_set_raises() -> None:
    env_without_cs = {k: v for k, v in VALID_OAUTH_ENV.items() if k != "TEST_OAUTH_CLIENT_SECRET"}
    with pytest.raises(ImapConfigError, match=r"TEST_OAUTH_CLIENT_SECRET.*not set"):
        ImapConfig.from_source_config(VALID_OAUTH_CFG, env=env_without_cs)


def test_oauth_doesnt_require_password_env() -> None:
    # OAuth config doesn't need password_env at all.
    cfg = {**VALID_OAUTH_CFG}
    cfg.pop("password_env", None)
    config = ImapConfig.from_source_config(cfg, env=VALID_OAUTH_ENV)
    assert config.auth_method == "oauth2"
    assert config.password == ""


def test_oauth_requires_oauth_provider_field() -> None:
    cfg = {k: v for k, v in VALID_OAUTH_CFG.items() if k != "oauth_provider"}
    with pytest.raises(ImapConfigError, match="oauth_provider"):
        ImapConfig.from_source_config(cfg, env=VALID_OAUTH_ENV)


def test_oauth_empty_provider_raises() -> None:
    cfg = {**VALID_OAUTH_CFG, "oauth_provider": ""}
    with pytest.raises(ImapConfigError, match="oauth_provider"):
        ImapConfig.from_source_config(cfg, env=VALID_OAUTH_ENV)


def test_oauth_unknown_provider_raises() -> None:
    cfg = {**VALID_OAUTH_CFG, "oauth_provider": "nope"}
    with pytest.raises(ImapConfigError, match="unknown oauth_provider"):
        ImapConfig.from_source_config(cfg, env=VALID_OAUTH_ENV)


def test_oauth_provider_microsoft_accepted_in_config() -> None:
    # "microsoft" is in the registry as a stub. Config validation should pass
    # even though actually using the provider raises NotImplementedError.
    cfg = {**VALID_OAUTH_CFG, "oauth_provider": "microsoft"}
    config = ImapConfig.from_source_config(cfg, env=VALID_OAUTH_ENV)
    assert config.oauth_provider == "microsoft"
