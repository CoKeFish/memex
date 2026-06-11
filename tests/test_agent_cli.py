"""CLI `memex` (interfaz del agente): despacha a las CLIs de dominio, cura el mantenimiento."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.agent_cli import main
from memex.db import connection
from memex.modules.bienestar.habits import add_habit


def _last_json(out: str) -> Any:
    return json.loads(out.strip().splitlines()[-1])


def test_help_lists_groups(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0
    out = capsys.readouterr().out
    assert "bienestar" in out
    assert "finance" in out
    assert "identidad" in out
    assert "calendario" in out
    assert "--event" in out
    # comandos del flujo de evento
    assert "start" in out
    assert "end" in out


def test_event_commands_are_recognized() -> None:
    assert main(["start"]) == 0
    assert main(["cancel"]) == 0  # descarta el abierto
    assert main(["end"]) == 1  # nada abierto ni cerrado → error de flujo


def test_no_args_shows_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "memex" in capsys.readouterr().out


def test_dispatch_bienestar_register(capsys: pytest.CaptureFixture[str]) -> None:
    with connection() as c:  # es para hábitos: el registro necesita un hábito que lo cubra
        add_habit(c, 1, name="Comer", cadence="daily", category="comida")
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


def test_dispatch_identidad_add(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["identidad", "add", "--name", "Ada", "--kind", "persona", "--json"])
    assert rc == 0
    row = _last_json(capsys.readouterr().out)
    assert row["kind"] == "persona"
    assert row["display_name"] == "Ada"
    with connection() as c:
        n = c.execute(text("SELECT count(*) FROM mod_identidades")).scalar_one()
    assert n == 1


def test_blocks_finance_maintenance() -> None:
    assert main(["finance", "consolidate"]) == 2  # mantenimiento, no del agente
    assert main(["finance", "dedup"]) == 2


def test_identidad_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    # El mantenimiento de identidades NO se expone al agente…
    assert main(["identidad", "sync", "--account", "1"]) == 2
    assert main(["identidad", "merge"]) == 2
    assert main(["identidad", "interest", "list"]) == 2
    assert main(["identidad", "help"]) == 0
    assert "add" in capsys.readouterr().out
    # …pero consulta/jerarquía/resolución SÍ (forwardean al CLI de dominio).
    assert main(["identidad", "search", "--q", "nadie"]) == 0
    assert "Sin resultados" in capsys.readouterr().out
    assert main(["identidad", "candidates"]) == 0
    assert "Sin candidatos" in capsys.readouterr().out
    assert main(["identidad", "tree"]) == 0
    assert "Sin jerarquía" in capsys.readouterr().out


def test_identidad_agente_set_parent_y_show(capsys: pytest.CaptureFixture[str]) -> None:
    # flujo del agente end-to-end por la superficie `memex`: add → set-parent → show
    assert (
        main(["identidad", "add", "--name", "Programa Z", "--kind", "organizacion", "--json"]) == 0
    )
    prog = _last_json(capsys.readouterr().out)["id"]
    assert (
        main(["identidad", "add", "--name", "Universidad Y", "--kind", "organizacion", "--json"])
        == 0
    )
    uni = _last_json(capsys.readouterr().out)["id"]
    assert main(["identidad", "set-parent", "--id", str(prog), "--parent", str(uni)]) == 0
    capsys.readouterr()
    assert main(["identidad", "show", "--id", str(prog), "--json"]) == 0
    ficha = _last_json(capsys.readouterr().out)
    assert ficha["parent_id"] == uni and ficha["parent_source"] == "agent"


def test_dispatch_calendario_add_and_list(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(
        [
            "calendario",
            "add",
            "--title",
            "Dentista",
            "--date",
            "2026-06-20",
            "--time",
            "09:00",
            "--json",
        ]
    )
    assert rc == 0
    row = _last_json(capsys.readouterr().out)
    assert row["instances"] == 1
    assert row["consolidated_ids"]

    rc = main(["calendario", "list", "--since", "2026-06-19", "--json"])
    assert rc == 0
    listed = _last_json(capsys.readouterr().out)
    assert [i["title"] for i in listed["items"]] == ["Dentista"]


def test_calendario_exposes_only_agent_commands(capsys: pytest.CaptureFixture[str]) -> None:
    # El mantenimiento (sync/oauth/ciclo) NO se expone al agente.
    assert main(["calendario", "pull", "--account", "1"]) == 2
    assert main(["calendario", "push", "--account", "1"]) == 2
    assert main(["calendario", "authorize", "--account", "1"]) == 2
    assert main(["calendario", "consolidate"]) == 2
    err = capsys.readouterr().err
    assert "memex-calendar-sync" in err  # el mensaje apunta a la CLI de mantenimiento real
    assert main(["calendario", "help"]) == 0
    assert "add" in capsys.readouterr().out


def test_unknown_group() -> None:
    assert main(["inventado"]) == 2
