from __future__ import annotations

from pathlib import Path

import pytest

from memex.ingestors.imap import oauth


def _google() -> oauth.OAuthProvider:
    return oauth.resolve("google")


def test_get_access_token_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    provider = _google()
    with pytest.raises(oauth.OAuthError):
        provider.get_access_token(token_path=missing)


def test_google_invalid_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json")
    provider = _google()
    with pytest.raises(oauth.OAuthError, match="failed to load"):
        provider.get_access_token(token_path=bad)


def test_google_token_file_not_found_message(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    provider = _google()
    with pytest.raises(oauth.OAuthError, match="not found"):
        provider.get_access_token(token_path=missing)
