"""OAuth provider registry for IMAP XOAUTH2 authentication.

Each provider implements the `OAuthProvider` Protocol defined here.
Providers are resolved by name through `resolve(name)`, following the same
lazy-loading pattern as `memex.sources.resolve()`.

The discipline: code outside this package types against `OAuthProvider`,
never against a concrete class like `GoogleOAuthProvider`. That lets us add
new providers (Microsoft, Yahoo, ...) by registering a loader here, without
touching `client.py` or `cli.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import ClassVar, Protocol, runtime_checkable


class OAuthError(Exception):
    """Raised when an OAuth flow or token refresh fails. Common to all providers."""


@runtime_checkable
class OAuthProvider(Protocol):
    """Anything that can do OAuth2 against an external identity provider for IMAP XOAUTH2.

    `name` matches the `oauth_provider` string in `sources.config`. Concrete
    implementations live in sibling modules and import their provider-specific
    libs internally — those libs never leak outside the implementation file.
    """

    name: ClassVar[str]

    def authorize_interactive(
        self,
        *,
        client_secret_path: str | Path,
        token_path: str | Path,
    ) -> None:
        """Run the interactive OAuth flow (browser-based consent), persist tokens to disk."""
        ...

    def get_access_token(
        self,
        *,
        token_path: str | Path,
    ) -> str:
        """Load tokens from disk, refresh if needed, return a fresh access_token string."""
        ...

    def get_access_token_from_json(self, *, token_json: str) -> str:
        """Como `get_access_token` pero desde un JSON del vault (self-contained). No toca disco.

        Lo usa el flujo web del dashboard, que guarda el token cifrado en el vault.
        """
        ...


def _google_loader() -> OAuthProvider:
    from memex.ingestors.imap.oauth.google import GoogleOAuthProvider

    return GoogleOAuthProvider()


def _microsoft_loader() -> OAuthProvider:
    from memex.ingestors.imap.oauth.microsoft import MicrosoftOAuthProvider

    return MicrosoftOAuthProvider()


_LAZY_PROVIDERS: dict[str, Callable[[], OAuthProvider]] = {
    "google": _google_loader,
    "microsoft": _microsoft_loader,
}


def resolve(name: str) -> OAuthProvider:
    """Return the provider instance for `name`, loading the module lazily.

    Raises `KeyError` if no provider is registered for `name`.
    """
    if name not in _LAZY_PROVIDERS:
        raise KeyError(
            f"no OAuth provider registered for name={name!r}. "
            f"Known: {sorted(_LAZY_PROVIDERS.keys())}"
        )
    return _LAZY_PROVIDERS[name]()


def known_providers() -> list[str]:
    """List provider names currently resolvable. Useful for config validation."""
    return sorted(_LAZY_PROVIDERS.keys())
