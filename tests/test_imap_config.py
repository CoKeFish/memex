"""Parsing de la ventana de fetch (incremental/range/last) en ImapConfig.from_source_config.

Puro (sin DB ni IMAP): valida que los overrides transitorios que inyecta el endpoint de fetch
se parsean a los tipos correctos y que los inválidos levantan ImapConfigError.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from memex.ingestors.imap.config import ImapConfig, ImapConfigError

ENV = {"U": "me@example.com", "P": "pw"}
BASE: dict[str, Any] = {
    "server": "imap.x.com",
    "auth": "basic",
    "username_env": "U",
    "password_env": "P",
    "folders": ["INBOX"],
}


def _cfg(**over: Any) -> dict[str, Any]:
    return {**BASE, **over}


def test_fetch_mode_defaults_incremental() -> None:
    cfg = ImapConfig.from_source_config(_cfg(), env=ENV)
    assert cfg.fetch_mode == "incremental"
    assert cfg.fetch_since is None
    assert cfg.fetch_until is None
    assert cfg.fetch_limit is None


def test_fetch_mode_last_parses_limit() -> None:
    cfg = ImapConfig.from_source_config(_cfg(fetch_mode="last", fetch_limit="5"), env=ENV)
    assert cfg.fetch_mode == "last"
    assert cfg.fetch_limit == 5


def test_fetch_mode_range_parses_dates() -> None:
    cfg = ImapConfig.from_source_config(
        _cfg(fetch_mode="range", fetch_since="2026-01-02", fetch_until="2026-02-01"), env=ENV
    )
    assert cfg.fetch_mode == "range"
    assert cfg.fetch_since == date(2026, 1, 2)
    assert cfg.fetch_until == date(2026, 2, 1)


def test_invalid_fetch_mode_raises() -> None:
    with pytest.raises(ImapConfigError):
        ImapConfig.from_source_config(_cfg(fetch_mode="bogus"), env=ENV)


def test_invalid_fetch_date_raises() -> None:
    with pytest.raises(ImapConfigError):
        ImapConfig.from_source_config(_cfg(fetch_mode="range", fetch_since="nope"), env=ENV)
