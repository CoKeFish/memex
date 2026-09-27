"""CLI `memex-tareas` end-to-end (DB de test, sin LLM) + despacho por `memex tareas` (agente) +
staging dentro de un evento multi-hecho."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from memex import agent_cli
from memex.db import connection
from memex.modules.tareas.cli import main


def _last_json(out: str) -> Any:
    return json.loads(out.strip().splitlines()[-1])


def _asunto(name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_asuntos (user_id, name, kind, status) "
                    "VALUES (1, :n, 'programa', 'abierto') RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def test_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0
    out = capsys.readouterr().out
    assert "add" in out and "dedup" in out.lower() and "--json" in out


def test_add_list_done_json_contract(capsys: pytest.CaptureFixture[str]) -> None:
    britano = _asunto("Programa Intensivo Britano")
    due = (date.today() + timedelta(days=3)).isoformat()
    rc = main(
        [
            "add",
            "--title",
            "Ejercicios página 42",
            "--due",
            due,
            "--asunto",
            "britano",
            "--source-text",
            "tarea del britano: ejercicios pág 42 para el jueves",
            "--json",
        ]
    )
    assert rc == 0
    row = _last_json(capsys.readouterr().out)
    assert row["deduped"] is False and row["due_on"] == due and row["asunto_id"] == britano
    assert row["metadata"]["source_text"].startswith("tarea del britano")

    # dedup por la CLI: misma tarea → deduped=true, no se crea otra
    assert main(["add", "--title", "ejercicios pagina 42", "--asunto", str(britano), "--json"]) == 0
    again = _last_json(capsys.readouterr().out)
    assert again["deduped"] is True and again["id"] == row["id"]

    assert main(["add", "--title", "Cambiar punto de atención EPS", "--json"]) == 0
    capsys.readouterr()
    assert main(["list", "--json"]) == 0
    lst = _last_json(capsys.readouterr().out)
    assert lst["count"] == 2
    assert [t["title"] for t in lst["items"]] == [
        "Ejercicios página 42",
        "Cambiar punto de atención EPS",
    ]

    assert main(["list", "--asunto", "britano", "--json"]) == 0
    assert _last_json(capsys.readouterr().out)["count"] == 1

    assert main(["done", "--id", str(row["id"]), "--json"]) == 0
    done = _last_json(capsys.readouterr().out)
    assert done["status"] == "completada" and done["done_at"]
    assert main(["list", "--json"]) == 0
    assert _last_json(capsys.readouterr().out)["count"] == 1


def test_update_show_cancel_and_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["add", "--title", "Renovar pasaporte", "--json"]) == 0
    tid = _last_json(capsys.readouterr().out)["id"]
    assert (
        main(["update", "--id", str(tid), "--due", "2026-12-01", "--priority", "alta", "--json"])
        == 0
    )
    upd = _last_json(capsys.readouterr().out)
    assert upd["due_on"] == "2026-12-01" and upd["priority"] == "alta"
    assert main(["update", "--id", str(tid), "--clear-due", "--json"]) == 0
    assert _last_json(capsys.readouterr().out)["due_on"] is None
    assert main(["show", "--id", str(tid)]) == 0
    assert "Renovar pasaporte" in capsys.readouterr().out
    assert main(["cancel", "--id", str(tid), "--json"]) == 0
    assert _last_json(capsys.readouterr().out)["status"] == "cancelada"
    # errores en JSON: última línea con error/message, exit 1
    assert main(["show", "--id", "999999", "--json"]) == 1
    err = _last_json(capsys.readouterr().out)
    assert err["error"] == "TareaNotFoundError"
    assert main(["add", "--title", "x", "--asunto", "no existe", "--json"]) == 1
    assert _last_json(capsys.readouterr().out)["error"] == "AsuntoNotFoundError"
    assert main(["add", "--title", "y", "--priority", "urgentísima"]) == 1


def test_agent_dispatch_and_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_cli.main(["tareas", "add", "--title", "Llamar al banco", "--json"]) == 0
    row = _last_json(capsys.readouterr().out)
    assert row["title"] == "Llamar al banco"
    assert agent_cli.main(["tareas", "rm", "--id", str(row["id"])]) != 0  # mantenimiento
    assert agent_cli.main(["help"]) == 0
    assert "memex tareas add" in capsys.readouterr().out


def test_agent_event_stages_tareas_and_end_persists(capsys: pytest.CaptureFixture[str]) -> None:
    assert agent_cli.main(["start", "--json"]) == 0
    ev = _last_json(capsys.readouterr().out)
    assert agent_cli.main(["tareas", "add", "--title", "Pagar matrícula", "--json"]) == 0
    staged = _last_json(capsys.readouterr().out)
    assert staged["event_id"] == ev["event_id"]
    with connection() as c:
        assert c.execute(text("SELECT count(*) FROM mod_tareas")).scalar() == 0  # encolada
    assert agent_cli.main(["end", "--json"]) == 0
    result = _last_json(capsys.readouterr().out)
    assert result["counts"]["tareas"] == 1 and result["tareas"][0]["title"] == "Pagar matrícula"
    with connection() as c:
        row = c.execute(text("SELECT title, event_id FROM mod_tareas")).mappings().one()
    assert row["event_id"] == ev["event_id"]
