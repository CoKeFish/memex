from __future__ import annotations

from pathlib import Path

import pytest

from memex.ingestors.imap import oauth


def test_known_providers_includes_google_and_microsoft() -> None:
    names = oauth.known_providers()
    assert "google" in names
    assert "microsoft" in names


def test_resolve_google_returns_provider_with_required_methods() -> None:
    provider = oauth.resolve("google")
    assert provider.name == "google"
    assert hasattr(provider, "authorize_interactive")
    assert hasattr(provider, "get_access_token")


def test_resolve_microsoft_returns_stub() -> None:
    provider = oauth.resolve("microsoft")
    assert provider.name == "microsoft"


def test_resolve_unknown_raises_keyerror() -> None:
    with pytest.raises(KeyError, match="yahoo"):
        oauth.resolve("yahoo")


def test_resolve_empty_string_raises_keyerror() -> None:
    with pytest.raises(KeyError):
        oauth.resolve("")


def test_microsoft_authorize_raises_not_implemented(tmp_path: Path) -> None:
    provider = oauth.resolve("microsoft")
    with pytest.raises(NotImplementedError, match="msal"):
        provider.authorize_interactive(
            client_secret_path=tmp_path / "cs.json",
            token_path=tmp_path / "token.json",
        )


def test_microsoft_get_access_token_raises_not_implemented(tmp_path: Path) -> None:
    provider = oauth.resolve("microsoft")
    with pytest.raises(NotImplementedError):
        provider.get_access_token(token_path=tmp_path / "token.json")


def test_resolve_satisfies_protocol_at_runtime() -> None:
    provider = oauth.resolve("google")
    assert isinstance(provider, oauth.OAuthProvider)
