"""CLI `memex-identidades` contra la DB de test: interest, accounts, candidates y sync (error)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.cli import main


def test_interest_add_list_remove(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["interest", "add", "--name", "Unity", "--domain", "unity.com"])
    assert rc == 0
    assert "Unity" in capsys.readouterr().out

    assert main(["interest", "list"]) == 0
    listed = capsys.readouterr().out
    assert "Unity" in listed and "unity.com" in listed

    with connection() as c:
        oid = c.execute(
            text(
                "SELECT id FROM mod_identidades "
                "WHERE user_id = 1 AND kind = 'organizacion' AND display_name = 'Unity'"
            )
        ).scalar_one()
    assert main(["interest", "remove", "--id", str(oid)]) == 0
    capsys.readouterr()  # descarta la salida del remove (que menciona 'Unity')
    assert main(["interest", "list"]) == 0
    assert "Unity" not in capsys.readouterr().out


def test_interest_add_is_idempotent_upsert(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["interest", "add", "--name", "Claude"]) == 0
    capsys.readouterr()
    assert main(["interest", "add", "--name", "Claude", "--alias", "claude.ai"]) == 0
    capsys.readouterr()
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT aliases FROM mod_identidades "
                "WHERE user_id = 1 AND kind = 'organizacion' AND display_name = 'Claude'"
            )
        ).all()
    assert len(rows) == 1  # upsert por nombre normalizado, no duplicó
    assert rows[0][0] == ["claude.ai"]


def test_accounts_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["accounts"]) == 0
    assert "Sin cuentas" in capsys.readouterr().out


def test_candidates_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["candidates"]) == 0
    assert "Sin candidatos" in capsys.readouterr().out


def test_sync_missing_account_is_error(capsys: pytest.CaptureFixture[str]) -> None:
    # Cuenta inexistente → run_sync cuenta el error y el CLI devuelve exit 1.
    assert main(["sync", "--account", "9999"]) == 1
