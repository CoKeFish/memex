"""CLI `memex` (interfaz del agente): despacha a las CLIs de dominio, cura el mantenimiento."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.agent_cli import main
from memex.db import connection


def _last_json(out: str) -> Any:
    return json.loads(out.strip().splitlines()[-1])


def test_help_lists_groups(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0
    out = capsys.readouterr().out
    assert "bienestar" in out
    assert "finance" in out
    assert "--event" in out


def test_no_args_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "memex" in capsys.readouterr().out


def test_dispatch_bienestar_register(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["bienestar", "register", "--category", "comida", "--activity", "almuerzo", "--json"])
    assert rc == 0
    row = _last_json(capsys.readouterr().out)
    assert row["category"] == "comida"
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM mod_bienestar_registros")).scalar_one()
    assert n == 1


def test_dispatch_finance_register(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["finance", "register", "--amount", "5", "--currency", "USD", "--json"])
    assert rc == 0
    row = _last_json(capsys.readouterr().out)
    assert row["amount"] == 5.0


def test_blocks_finance_maintenance() -> None:
    assert main(["finance", "consolidate"]) == 2  # mantenimiento, no del agente
    assert main(["finance", "dedup"]) == 2


def test_unknown_group() -> None:
    assert main(["inventado"]) == 2
