"""CLI `memex-bienestar` end-to-end (DB de test, sin LLM): register/list/summary + `--json`."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.bienestar.cli import main


def _last_json(out: str) -> Any:
    """El JSON de la CLI es la ÚLTIMA línea de stdout (los logs van en líneas previas). Contrato de
    `--json` para el agente: parsear la última línea de stdout."""
    return json.loads(out.strip().splitlines()[-1])


def test_cli_register_inserts() -> None:
    rc = main(
        ["register", "--category", "comida", "--activity", "almuerzo", "--description", "pizza"]
    )
    assert rc == 0
    with connection() as c:
        row = (
            c.execute(text("SELECT category, activity, description FROM mod_bienestar_registros"))
            .mappings()
            .one()
        )
    assert row["category"] == "comida"
    assert row["activity"] == "almuerzo"
    assert row["description"] == "pizza"


def test_cli_register_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["register", "--category", "ejercicio", "--activity", "gimnasio", "--json"])
    assert rc == 0
    out = _last_json(capsys.readouterr().out)
    assert out["category"] == "ejercicio"
    assert isinstance(out["id"], int)


def test_cli_invalid_category_becomes_otros() -> None:
    assert main(["register", "--category", "inventada", "--activity", "x"]) == 0
    with connection() as c:
        cat = c.execute(text("SELECT category FROM mod_bienestar_registros")).scalar_one()
    assert cat == "otros"


def test_cli_register_detail_json() -> None:
    rc = main(
        [
            "register",
            "--category",
            "comida",
            "--activity",
            "almuerzo",
            "--detail",
            '{"calories": 800}',
        ]
    )
    assert rc == 0
    with connection() as c:
        detail = c.execute(text("SELECT detail FROM mod_bienestar_registros")).scalar_one()
    assert detail == {"calories": 800}


def test_cli_summary_json(capsys: pytest.CaptureFixture[str]) -> None:
    main(["register", "--category", "comida", "--activity", "almuerzo"])
    main(["register", "--category", "comida", "--activity", "cena"])
    capsys.readouterr()  # descartar lo impreso por los register
    rc = main(["summary", "--json"])
    assert rc == 0
    s = _last_json(capsys.readouterr().out)
    assert s["total"] == 2
    assert s["by_category"]["comida"] == 2


def test_cli_list_json(capsys: pytest.CaptureFixture[str]) -> None:
    main(["register", "--category", "salud", "--activity", "ibuprofeno"])
    capsys.readouterr()
    rc = main(["list", "--json"])
    assert rc == 0
    rows = _last_json(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["activity"] == "ibuprofeno"


def test_cli_habit_add_list_rm() -> None:
    rc = main(["habit", "add", "--name", "Gym", "--cadence", "daily", "--activity", "gimnasio"])
    assert rc == 0
    with connection() as c:
        hid = c.execute(text("SELECT id FROM mod_bienestar_habits WHERE user_id = 1")).scalar_one()
    assert main(["habit", "list"]) == 0
    assert main(["habit", "rm", "--id", str(hid)]) == 0
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM mod_bienestar_habits")).scalar_one()
    assert n == 0


def test_cli_help(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["help"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "memex-bienestar" in out
    assert "register" in out
    assert "--event" in out


def test_cli_adherence_json(capsys: pytest.CaptureFixture[str]) -> None:
    main(["habit", "add", "--name", "Gym", "--cadence", "daily", "--activity", "gimnasio"])
    main(["register", "--category", "ejercicio", "--activity", "gimnasio"])  # ahora → cuenta hoy
    capsys.readouterr()
    rc = main(["adherence", "--json"])
    assert rc == 0
    rows = _last_json(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["habit"]["name"] == "Gym"
    assert rows[0]["current"] >= 1
