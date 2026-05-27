from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from memex.core.source import SourceConfigError


class ImapConfigError(SourceConfigError):
    """Raised when an IMAP source config is invalid or env-var-resolved values are missing.

    Subclasses `SourceConfigError` so callers can catch the generic base and
    handle any source's config failure uniformly.
    """


AuthMethod = Literal["basic", "oauth2"]


@dataclass(frozen=True)
class ImapConfig:
    """Resolved IMAP configuration for a single source.

    Two authentication modes:

    - ``"basic"`` (default): username + password from env vars. The env var
      *names* live in `sources.config` under `username_env` / `password_env`;
      the secret values never touch the DB.
    - ``"oauth2"``: XOAUTH2 SASL using OAuth2 tokens from an external identity
      provider. Which provider is determined by `oauth_provider` in
      `sources.config` (e.g. ``"google"``). `username_env` points to the
      account email. Two file paths (from env vars
      `oauth_client_secret_path_env` and `oauth_token_path_env`) point to the
      OAuth client_secret.json and the persisted token.json respectively.

    See ADR-001 for the rationale of keeping secrets out of the DB.
    """

    server: str
    port: int
    username: str
    auth_method: AuthMethod
    folders: list[str]

    # Basic auth fields.
    password: str = ""

    # OAuth2 fields (paths on disk; the files themselves contain the secrets).
    oauth_provider: str = ""
    oauth_client_secret_path: str = ""
    oauth_token_path: str = ""

    # Tunables.
    since_days: int = 7
    batch_size: int = 50
    fetch_body: bool = True
    max_body_bytes: int = 524288
    use_ssl: bool = True

    # Carry env-var *names* (not values) for logging and debugging.
    username_env: str = ""
    password_env: str = ""
    oauth_client_secret_path_env: str = ""
    oauth_token_path_env: str = ""

    def __repr__(self) -> str:
        return (
            "ImapConfig("
            f"server={self.server!r}, port={self.port}, "
            f"username={self.username!r}, auth_method={self.auth_method!r}, "
            f"password=<redacted>, "
            f"oauth_provider={self.oauth_provider!r}, "
            f"oauth_client_secret_path={self.oauth_client_secret_path!r}, "
            f"oauth_token_path={self.oauth_token_path!r}, "
            f"folders={self.folders!r}, since_days={self.since_days}, "
            f"batch_size={self.batch_size}, fetch_body={self.fetch_body}, "
            f"max_body_bytes={self.max_body_bytes}, use_ssl={self.use_ssl})"
        )

    @classmethod
    def from_source_config(
        cls,
        cfg: dict[str, Any],
        env: Mapping[str, str] | None = None,
    ) -> ImapConfig:
        env_map: Mapping[str, str] = env if env is not None else os.environ

        if "server" not in cfg:
            raise ImapConfigError("missing 'server' in sources.config")
        server = str(cfg["server"]).strip()
        if not server:
            raise ImapConfigError("'server' is empty")

        port = int(cfg.get("port", 993))
        auth_method_raw = cfg.get("auth", "basic")
        if auth_method_raw not in ("basic", "oauth2"):
            raise ImapConfigError(f"'auth' must be 'basic' or 'oauth2', got {auth_method_raw!r}")
        auth_method = cast("AuthMethod", auth_method_raw)

        # username_env is required for both modes (in OAuth, the email is part of
        # the SASL XOAUTH2 string).
        username_env = cfg.get("username_env")
        if not username_env:
            raise ImapConfigError(
                "sources.config must reference the account identity via 'username_env'."
            )
        username = _require_env(env_map, str(username_env))

        folders_raw = cfg.get("folders", ["INBOX"])
        if not isinstance(folders_raw, list) or not folders_raw:
            raise ImapConfigError("'folders' must be a non-empty list")
        folders = [str(f) for f in folders_raw]

        common_kwargs: dict[str, Any] = {
            "server": server,
            "port": port,
            "username": username,
            "auth_method": auth_method,
            "folders": folders,
            "since_days": int(cfg.get("since_days", 7)),
            "batch_size": int(cfg.get("batch_size", 50)),
            "fetch_body": bool(cfg.get("fetch_body", True)),
            "max_body_bytes": int(cfg.get("max_body_bytes", 524288)),
            "use_ssl": bool(cfg.get("use_ssl", True)),
            "username_env": str(username_env),
        }

        if auth_method == "basic":
            password_env = cfg.get("password_env")
            if not password_env:
                raise ImapConfigError("auth='basic' requires 'password_env' in sources.config.")
            password = _require_env(env_map, str(password_env))
            return cls(
                **common_kwargs,
                password=password,
                password_env=str(password_env),
            )

        # auth_method == "oauth2"
        # Import lazily to avoid pulling the oauth registry (and any future
        # provider-specific libs) when only basic auth is used.
        from memex.ingestors.imap import oauth as oauth_registry

        provider = cfg.get("oauth_provider")
        if not provider:
            raise ImapConfigError(
                "auth='oauth2' requires 'oauth_provider' in sources.config "
                f"(known: {oauth_registry.known_providers()})."
            )
        provider_str = str(provider)
        if provider_str not in oauth_registry.known_providers():
            raise ImapConfigError(
                f"unknown oauth_provider={provider_str!r}. "
                f"Known: {oauth_registry.known_providers()}"
            )

        cs_env = cfg.get("oauth_client_secret_path_env")
        token_env = cfg.get("oauth_token_path_env")
        if not cs_env or not token_env:
            raise ImapConfigError(
                "auth='oauth2' requires 'oauth_client_secret_path_env' and "
                "'oauth_token_path_env' in sources.config."
            )
        client_secret_path = _require_env(env_map, str(cs_env))
        token_path = _require_env(env_map, str(token_env))
        return cls(
            **common_kwargs,
            oauth_provider=provider_str,
            oauth_client_secret_path=client_secret_path,
            oauth_token_path=token_path,
            oauth_client_secret_path_env=str(cs_env),
            oauth_token_path_env=str(token_env),
        )


def _require_env(env_map: Mapping[str, str], var: str) -> str:
    if var not in env_map:
        raise ImapConfigError(f"env var {var!r} is not set")
    value = env_map[var].strip()
    if not value:
        raise ImapConfigError(f"env var {var!r} resolves to empty value")
    return value
