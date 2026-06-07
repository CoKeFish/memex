"""Flujo de evento multi-hecho del agente: start → register* (staged) → end (procesa atómico).

Se verifica por el CLI umbrella `memex` (la superficie real): staging (no persiste hasta `end`),
cierre dependency-aware (finanzas ata la contraparte por id de la identidad del evento, no NULL),
atomicidad (rollback total ante un hecho inválido), idempotencia de `end`, un evento abierto por
user, y que el registro INMEDIATO (sin evento) sigue funcionando.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.agent_cli import main
from memex.db import connection


def _last_json(out: str) -> Any:
    return json.loads(out.strip().splitlines()[-1])


def _scalar(sql: str, **params: Any) -> Any:
    with connection() as c:
        return c.execute(text(sql), params).scalar()


def _open_event_id(user_id: int = 1) -> str | None:
    val = _scalar(
        "SELECT event_id FROM mod_agent_event WHERE user_id = :u AND status = 'open'", u=user_id
    )
    return None if val is None else str(val)


def _stage_invoice() -> None:
    """start + los 3 register de una factura (identidad/gasto/comida), aún SIN cerrar."""
    assert main(["start"]) == 0
    assert main(["identidad", "add", "--name", "Rest La Esquina", "--kind", "organizacion"]) == 0
    assert (
        main(
            [
                "finance",
                "register",
                "--amount",
                "50000",
                "--currency",
                "COP",
                "--counterparty",
                "Rest La Esquina",
            ]
        )
        == 0
    )
    assert main(["bienestar", "register", "--category", "comida", "--activity", "almuerzo"]) == 0


def test_register_during_event_is_staged_not_persisted(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start"]) == 0
    capsys.readouterr()
    assert main(["finance", "register", "--amount", "5", "--currency", "USD", "--json"]) == 0
    staged = _last_json(capsys.readouterr().out)
    assert staged["staged"] is True and staged["kind"] == "finance"
    # NO tocó las tablas de dominio; sí encoló en el staging.
    assert _scalar("SELECT count(*) FROM mod_finance_transactions") == 0
    assert _scalar("SELECT count(*) FROM mod_agent_event_facts") == 1


def test_read_only_passes_through_during_event() -> None:
    assert main(["start"]) == 0
    # list es read-only → NO se encola, corre inmediato.
    assert main(["bienestar", "list", "--json"]) == 0
    assert _scalar("SELECT count(*) FROM mod_agent_event_facts") == 0


def test_end_processes_event_and_links_counterparty(capsys: pytest.CaptureFixture[str]) -> None:
    _stage_invoice()
    assert _scalar("SELECT count(*) FROM mod_finance_transactions") == 0  # nada todavía
    event_id = _open_event_id()
    capsys.readouterr()
    assert main(["end", "--json"]) == 0
    result = _last_json(capsys.readouterr().out)
    assert result["counts"] == {"identidad": 1, "finance": 1, "bienestar": 1}

    with connection() as c:
        org_id = c.execute(
            text(
                "SELECT id FROM mod_identidades WHERE user_id = 1 AND kind = 'organizacion' "
                "AND display_name = 'Rest La Esquina'"
            )
        ).scalar_one()
        # el FK de finanzas quedó atado por ID (NO NULL) a la identidad del MISMO evento
        cid = c.execute(
            text("SELECT counterparty_identity_id FROM mod_finance_transactions WHERE user_id = 1")
        ).scalar_one()
        assert cid == org_id
        contraparte = c.execute(
            text(
                "SELECT count(*) FROM relation_edges WHERE user_id = 1 "
                "AND relation_type = 'contraparte' AND producer = 'finance'"
            )
        ).scalar_one()
        assert contraparte == 1
        mismo = c.execute(
            text(
                "SELECT count(*) FROM relation_edges WHERE user_id = 1 "
                "AND relation_type = 'mismo_evento'"
            )
        ).scalar_one()
        assert mismo >= 1
        beid = c.execute(
            text("SELECT event_id FROM mod_bienestar_registros WHERE user_id = 1")
        ).scalar_one()
        assert beid == event_id
    assert _open_event_id() is None  # el evento quedó cerrado


def test_end_is_atomic_rolls_back_on_invalid_fact(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start"]) == 0
    assert main(["identidad", "add", "--name", "ACME", "--kind", "organizacion"]) == 0
    # monto inválido: argparse lo acepta (string), Decimal revienta en el cierre.
    assert main(["finance", "register", "--amount", "basura", "--currency", "USD"]) == 0
    capsys.readouterr()
    assert main(["end"]) == 1  # falla → rollback total
    # nada persistió (ni la identidad ya procesada) y el evento sigue ABIERTO (reintentable)
    assert _scalar("SELECT count(*) FROM mod_identidades WHERE user_id = 1") == 0
    assert _scalar("SELECT count(*) FROM mod_finance_transactions WHERE user_id = 1") == 0
    assert _open_event_id() is not None
    assert _scalar("SELECT count(*) FROM mod_agent_event_facts") == 2


def test_end_is_idempotent_on_retry(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start"]) == 0
    assert main(["bienestar", "register", "--category", "comida", "--activity", "cena"]) == 0
    assert main(["end"]) == 0
    assert _scalar("SELECT count(*) FROM mod_bienestar_registros WHERE user_id = 1") == 1
    capsys.readouterr()
    # reintentar end (ya no hay evento abierto) → devuelve lo guardado, NO re-inserta
    assert main(["end", "--json"]) == 0
    result = _last_json(capsys.readouterr().out)
    assert result.get("already_closed") is True
    assert _scalar("SELECT count(*) FROM mod_bienestar_registros WHERE user_id = 1") == 1


def test_one_open_event_per_user(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["start"]) == 0
    assert main(["start"]) == 1  # ya hay uno abierto
    assert "abierto" in capsys.readouterr().err
    assert main(["cancel"]) == 0
    assert _open_event_id() is None
    assert main(["start"]) == 0  # tras cancelar, se puede abrir otro


def test_cancel_discards_staged_facts() -> None:
    assert main(["start"]) == 0
    assert main(["bienestar", "register", "--category", "comida", "--activity", "x"]) == 0
    assert _scalar("SELECT count(*) FROM mod_agent_event_facts") == 1
    assert main(["cancel"]) == 0
    assert _scalar("SELECT count(*) FROM mod_agent_event_facts") == 0
    assert _scalar("SELECT count(*) FROM mod_bienestar_registros") == 0


def test_immediate_register_without_event_persists(capsys: pytest.CaptureFixture[str]) -> None:
    # sin start, el register persiste como siempre (backward-compat).
    assert main(["bienestar", "register", "--category", "comida", "--activity", "x", "--json"]) == 0
    row = _last_json(capsys.readouterr().out)
    assert row["category"] == "comida"
    assert _scalar("SELECT count(*) FROM mod_bienestar_registros") == 1


def test_counterparty_without_matching_identity_stays_null() -> None:
    # gasto cuyo comercio NO está en el evento ni en el directorio → FK NULL (resultado correcto).
    assert main(["start"]) == 0
    assert (
        main(
            [
                "finance",
                "register",
                "--amount",
                "9",
                "--currency",
                "USD",
                "--counterparty",
                "Desconocido",
            ]
        )
        == 0
    )
    assert main(["end"]) == 0
    cid = _scalar("SELECT counterparty_identity_id FROM mod_finance_transactions WHERE user_id = 1")
    assert cid is None


def test_end_is_order_independent(capsys: pytest.CaptureFixture[str]) -> None:
    # el agente registra en orden "equivocado" (bienestar → finance → identidad); el cierre ORDENA
    # por dependencia (identidad→finance→bienestar), así que el resultado es idéntico.
    assert main(["start"]) == 0
    assert main(["bienestar", "register", "--category", "comida", "--activity", "almuerzo"]) == 0
    assert (
        main(
            [
                "finance",
                "register",
                "--amount",
                "50000",
                "--currency",
                "COP",
                "--counterparty",
                "Rest La Esquina",
            ]
        )
        == 0
    )
    assert main(["identidad", "add", "--name", "Rest La Esquina", "--kind", "organizacion"]) == 0
    capsys.readouterr()
    assert main(["end", "--json"]) == 0
    result = _last_json(capsys.readouterr().out)
    assert result["counts"] == {"identidad": 1, "finance": 1, "bienestar": 1}
    with connection() as c:
        org_id = c.execute(
            text(
                "SELECT id FROM mod_identidades WHERE user_id = 1 AND kind = 'organizacion' "
                "AND display_name = 'Rest La Esquina'"
            )
        ).scalar_one()
        cid = c.execute(
            text("SELECT counterparty_identity_id FROM mod_finance_transactions WHERE user_id = 1")
        ).scalar_one()
        assert cid == org_id  # atado por id pese al orden de registro
        contraparte = c.execute(
            text(
                "SELECT count(*) FROM relation_edges WHERE user_id = 1 "
                "AND relation_type = 'contraparte' AND producer = 'finance'"
            )
        ).scalar_one()
        assert contraparte == 1
        mismo = c.execute(
            text(
                "SELECT count(*) FROM relation_edges WHERE user_id = 1 "
                "AND relation_type = 'mismo_evento'"
            )
        ).scalar_one()
        assert mismo >= 1
