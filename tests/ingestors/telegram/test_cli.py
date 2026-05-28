"""Smoke tests del CLI memex-telegram.

Verifica parsing de argumentos y comportamiento básico de cada subcomando con
mocks sobre `MemexServerClient` (HTTP) y `TelegramClientWrapper` (Telethon).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from memex.ingestors.telegram.cli import main


def _parser() -> Any:
    from memex.ingestors.telegram.cli import _build_parser

    return _build_parser()


def test_help_for_each_subcommand() -> None:
    p = _parser()
    # subparsers action exposes `choices` as the dict of subcommand names.
    subs = p._subparsers._group_actions[0].choices
    assert set(subs.keys()) == {"auth", "run", "discover"}


def test_run_with_no_telegram_sources_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get_sources_by_type.return_value = []

    with patch("memex.ingestors.telegram.cli.MemexServerClient", return_value=fake_client):
        rc = main(["run"])
    assert rc == 0
    fake_client.get_sources_by_type.assert_called_once_with("telegram")


def test_run_with_unknown_source_id_returns_one(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get_sources_by_type.return_value = []

    with patch("memex.ingestors.telegram.cli.MemexServerClient", return_value=fake_client):
        rc = main(["run", "--source-id", "999"])
    assert rc == 1


def test_discover_with_unknown_source_id_returns_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get_sources_by_type.return_value = []

    with patch("memex.ingestors.telegram.cli.MemexServerClient", return_value=fake_client):
        rc = main(["discover", "--source-id", "999"])
    assert rc == 1


def test_discover_calls_iter_dialogs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_client = MagicMock()
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = None
    fake_client.get_sources_by_type.return_value = [
        {
            "id": 1,
            "user_id": 1,
            "name": "tg-test",
            "config": {},
        }
    ]

    class _FakeDialog:
        def __init__(self, did: int, name: str, is_group: bool = True) -> None:
            self.id = did
            self.name = name
            self.is_group = is_group
            self.is_channel = False
            self.is_user = False

    class _FakeTC:
        def __init__(self, cfg: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeTC:
            return self

        async def __aexit__(self, *a: Any) -> None:
            pass

        async def iter_dialogs(self) -> AsyncIterator[Any]:
            for d in (_FakeDialog(-100, "Group A"), _FakeDialog(-200, "Group B")):
                yield d

    env = {
        "MEMEX_TG_API_ID": "12345",
        "MEMEX_TG_API_HASH": "dead",
        "MEMEX_TG_PHONE": "+34999",
    }
    with (
        patch("memex.ingestors.telegram.cli.MemexServerClient", return_value=fake_client),
        patch("memex.ingestors.telegram.cli.TelegramClientWrapper", _FakeTC),
        patch.dict("os.environ", env, clear=False),
    ):
        rc = main(["discover", "--source-id", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Group A" in out
    assert "Group B" in out
    assert "-100" in out


def test_main_is_callable() -> None:
    """Sanidad: main() es importable y callable."""
    assert callable(main)
