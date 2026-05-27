from __future__ import annotations

from pathlib import Path

import pytest

from memex.ingestors.imap.oauth import (
    OAuthError,
    get_access_token,
    load_and_refresh,
)


def test_load_and_refresh_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(OAuthError, match="not found"):
        load_and_refresh(missing)


def test_load_and_refresh_invalid_json_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json")
    with pytest.raises(OAuthError, match="failed to load"):
        load_and_refresh(bad)


def test_get_access_token_missing_file_raises(tmp_path: Path) -> None:
    missing = tmp_path / "nope.json"
    with pytest.raises(OAuthError):
        get_access_token(missing)
