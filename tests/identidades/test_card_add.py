"""CLI `memex-identidades add` — resolve-or-create de una identidad desde una tarjeta de contacto.

Verifica: crea nueva / resuelve existente (no duplica) / resuelve por email (señal fuerte) /
teléfono como identificador idempotente / persona+org teje la afiliación y la arista `afiliado` /
`--json` en la última línea / `--org` exige `--kind persona`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.cli import main


def _add(*flags: str) -> int:
    return main(["add", *flags])


def _last_json(out: str) -> Any:
    return json.loads(out.strip().splitlines()[-1])


def test_add_creates_new_persona(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _add("--name", "Ada Lovelace", "--kind", "persona", "--email", "ada@x.com", "--json")
    assert rc == 0
    row = _last_json(capsys.readouterr().out)
    assert row["kind"] == "persona"
    assert row["display_name"] == "Ada Lovelace"
    assert row["source"] == "manual"
    assert row["method"] == "created"
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_identidades WHERE user_id = 1 AND kind = 'persona'")
        ).scalar_one()
    assert n == 1


def test_add_is_idempotent_resolves_existing(capsys: pytest.CaptureFixture[str]) -> None:
    assert _add("--name", "Bob Builder", "--kind", "persona", "--json") == 0
    capsys.readouterr()
    assert _add("--name", "Bob Builder", "--kind", "persona", "--json") == 0
    row = _last_json(capsys.readouterr().out)
    assert row["method"] != "created"  # resolvió a la existente, no creó otra
    with connection() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM mod_identidades "
                "WHERE user_id = 1 AND display_name = 'Bob Builder'"
            )
        ).scalar_one()
    assert n == 1  # no duplicó


def test_add_resolves_by_email_strong_signal(capsys: pytest.CaptureFixture[str]) -> None:
    assert _add("--name", "Ada L.", "--kind", "persona", "--email", "ada@x.com", "--json") == 0
    capsys.readouterr()
    # Mismo email, nombre distinto → resuelve por email (señal fuerte), no crea otra.
    assert (
        _add("--name", "Ada Lovelace", "--kind", "persona", "--email", "ada@x.com", "--json") == 0
    )
    row = _last_json(capsys.readouterr().out)
    assert row["method"] == "email"
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM mod_identidades WHERE user_id = 1 AND kind = 'persona'")
        ).scalar_one()
    assert n == 1


def test_add_phone_stored_and_idempotent() -> None:
    assert _add("--name", "Carol", "--kind", "persona", "--phone", "+57 300 123 4567") == 0
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT value_norm FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = 'phone'"
            )
        ).all()
    assert len(rows) == 1
    # Re-correr con la misma tarjeta no duplica el identificador.
    assert _add("--name", "Carol", "--kind", "persona", "--phone", "+57 300 123 4567") == 0
    with connection() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = 'phone'"
            )
        ).scalar_one()
    assert n == 1


def test_add_person_with_org_links_affiliation(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _add(
        "--name", "Dana", "--kind", "persona", "--org", "Acme Corp", "--role", "CTO", "--json"
    )
    assert rc == 0
    row = _last_json(capsys.readouterr().out)
    assert row["kind"] == "persona"
    assert row["org"]["display_name"] == "Acme Corp"
    assert row["org"]["kind"] == "organizacion"
    assert row["org"]["role"] == "CTO"
    person_id, org_id = row["id"], row["org"]["id"]
    with connection() as c:
        po = c.execute(
            text(
                "SELECT role FROM mod_identidades_person_orgs "
                "WHERE user_id = 1 AND person_id = :p AND org_id = :o"
            ),
            {"p": person_id, "o": org_id},
        ).scalar_one()
        assert po == "CTO"
        edge = (
            c.execute(
                text(
                    """
                    SELECT producer, status FROM relation_edges
                    WHERE user_id = 1 AND relation_type = 'afiliado'
                      AND src_slug = 'identidades:person' AND src_id = :p
                      AND dst_slug = 'identidades:org' AND dst_id = :o
                    """
                ),
                {"p": person_id, "o": org_id},
            )
            .mappings()
            .one()
        )
    assert edge["producer"] == "identidades"
    assert edge["status"] == "confirmed"


def test_add_org_requires_persona(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _add("--name", "Foo", "--kind", "organizacion", "--org", "Bar")
    assert rc == 1
    assert "persona" in capsys.readouterr().err


def test_help_lists_add(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["help"]) == 0
    out = capsys.readouterr().out
    assert "add" in out
    assert "resolve-or-create" in out
