"""CLI `memex-identidades` contra la DB de test: interest, accounts, candidates, sync (error) y
los comandos del agente (search/show/tree/set-parent/annotate/resolve)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.cli import main


def _last_json(out: str) -> dict[str, Any]:
    """La fila pública es la ÚLTIMA línea de stdout (las previas son logs)."""
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    return dict(json.loads(lines[-1]))


def _mk(kind: str, name: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades (user_id, kind, display_name) "
                    "VALUES (1, :k, :n) RETURNING id"
                ),
                {"k": kind, "n": name},
            ).scalar_one()
        )


def _mk_candidate(a: int, b: int) -> int:
    lo, hi = min(a, b), max(a, b)
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO mod_identidades_merge_candidates "
                    "(user_id, identity_a_id, identity_b_id, reason, score) "
                    "VALUES (1, :a, :b, 'trgm_name', 0.7) RETURNING id"
                ),
                {"a": lo, "b": hi},
            ).scalar_one()
        )


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


def test_interest_list_incluye_productos(capsys: pytest.CaptureFixture[str]) -> None:
    # una entidad reclasificada a producto con interest=TRUE sigue saliendo en el listado
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, interest, source) "
                "VALUES (1,'producto','Steam',TRUE,'manual')"
            )
        )
    assert main(["interest", "list"]) == 0
    assert "Steam" in capsys.readouterr().out


def test_interest_add_reusa_producto_existente(capsys: pytest.CaptureFixture[str]) -> None:
    # si la entidad ya existe como producto, el add actualiza su interés (no duplica como org)
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, interest, source) "
                "VALUES (1,'producto','Recraft',FALSE,'extraction')"
            )
        )
    assert main(["interest", "add", "--name", "Recraft"]) == 0
    capsys.readouterr()
    with connection() as c:
        rows = c.execute(
            text(
                "SELECT kind, interest FROM mod_identidades "
                "WHERE user_id = 1 AND display_name = 'Recraft'"
            )
        ).all()
    assert len(rows) == 1  # no se creó una org duplicada
    assert (rows[0][0], rows[0][1]) == ("producto", True)


def test_accounts_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["accounts"]) == 0
    assert "Sin cuentas" in capsys.readouterr().out


def test_candidates_empty(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["candidates"]) == 0
    assert "Sin candidatos" in capsys.readouterr().out


def test_sync_missing_account_is_error(capsys: pytest.CaptureFixture[str]) -> None:
    # Cuenta inexistente → run_sync cuenta el error y el CLI devuelve exit 1.
    assert main(["sync", "--account", "9999"]) == 1


# ----- comandos del agente: search / show / tree --------------------------------- #


def test_search_por_nombre_alias_e_identificador(capsys: pytest.CaptureFixture[str]) -> None:
    pid = _mk("persona", "Roy Monroy")
    with connection() as c:
        c.execute(
            text("UPDATE mod_identidades SET aliases = ARRAY['El Romo'] WHERE id = :i"), {"i": pid}
        )
        c.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm, source) "
                "VALUES (1, :i, 'email', 'email', 'Roy@x.com', 'roy@x.com', 'manual')"
            ),
            {"i": pid},
        )
    _mk("organizacion", "Otra Cosa")

    assert main(["search", "--q", "monroy"]) == 0
    assert "Roy Monroy" in capsys.readouterr().out
    assert main(["search", "--q", "el romo"]) == 0  # por alias
    assert "Roy Monroy" in capsys.readouterr().out
    assert main(["search", "--q", "roy@x.com", "--json"]) == 0  # por identificador
    data = _last_json(capsys.readouterr().out)
    assert data["count"] == 1 and data["items"][0]["id"] == pid
    assert main(["search", "--q", "monroy", "--kind", "organizacion", "--json"]) == 0
    assert _last_json(capsys.readouterr().out)["count"] == 0  # el filtro por kind excluye


def test_show_ficha_completa(capsys: pytest.CaptureFixture[str]) -> None:
    org = _mk("organizacion", "Universidad Y")
    sub = _mk("organizacion", "Programa Z")
    otra = _mk("organizacion", "Universidad Y S.A.")
    persona = _mk("persona", "Ada")
    with connection() as c:
        c.execute(
            text("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :i"),
            {"p": org, "i": sub},
        )
        c.execute(
            text(
                "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id, role) "
                "VALUES (1, :per, :org, 'estudiante')"
            ),
            {"per": persona, "org": org},
        )
    _mk_candidate(org, otra)

    assert main(["show", "--id", str(org)]) == 0
    out = capsys.readouterr().out
    assert "Universidad Y" in out
    assert "Programa Z" in out  # sub
    assert "Ada" in out  # afiliación
    assert "Universidad Y S.A." in out  # candidato pendiente
    assert "resolve" in out

    assert main(["show", "--id", str(sub), "--json"]) == 0
    ficha = _last_json(capsys.readouterr().out)
    assert ficha["parent_id"] == org and ficha["parent_name"] == "Universidad Y"
    assert ficha["merge_candidates"] == []  # el candidato es de la org, no del sub

    assert main(["show", "--id", "99999"]) == 1  # inexistente


def test_tree_bosque_y_subarbol(capsys: pytest.CaptureFixture[str]) -> None:
    uni = _mk("organizacion", "Universidad Y")
    prog = _mk("organizacion", "Programa Z")
    prod = _mk("producto", "Steam")
    valve = _mk("organizacion", "Valve")
    _mk("organizacion", "Suelta SA")
    with connection() as c:
        for child, parent in ((prog, uni), (prod, valve)):
            c.execute(
                text("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :i"),
                {"p": parent, "i": child},
            )

    assert main(["tree"]) == 0
    out = capsys.readouterr().out
    assert "Universidad Y" in out and "Programa Z" in out
    assert "Steam" in out and "[producto]" in out
    assert "Suelta SA" not in out  # sin hijos no aparece (va al conteo)
    assert "1 entradas sin jerarquía" in out

    assert main(["tree", "--id", str(uni), "--json"]) == 0
    node = _last_json(capsys.readouterr().out)
    assert node["id"] == uni and node["children"][0]["id"] == prog


# ----- set-parent / annotate ------------------------------------------------------ #


def test_set_parent_clear_y_ciclo(capsys: pytest.CaptureFixture[str]) -> None:
    uni = _mk("organizacion", "Universidad Y")
    prog = _mk("organizacion", "Programa Z")

    assert main(["set-parent", "--id", str(prog), "--parent", str(uni), "--json"]) == 0
    row = _last_json(capsys.readouterr().out)
    assert row["parent_id"] == uni and row["parent_source"] == "agent"
    with connection() as c:
        meta = c.execute(
            text(
                "SELECT parent_identity_id, metadata->>'parent_source' "
                "FROM mod_identidades WHERE id = :i"
            ),
            {"i": prog},
        ).first()
    assert meta is not None and (int(meta[0]), meta[1]) == (uni, "agent")

    # ciclo: la uni no puede colgar de su propio sub
    assert main(["set-parent", "--id", str(uni), "--parent", str(prog)]) == 1
    assert "ciclo" in capsys.readouterr().err
    # self-parent
    assert main(["set-parent", "--id", str(uni), "--parent", str(uni)]) == 1
    capsys.readouterr()
    # padre inexistente
    assert main(["set-parent", "--id", str(prog), "--parent", "99999"]) == 1
    capsys.readouterr()

    assert main(["set-parent", "--id", str(prog), "--clear"]) == 0
    capsys.readouterr()
    with connection() as c:
        assert (
            c.execute(
                text("SELECT parent_identity_id FROM mod_identidades WHERE id = :i"), {"i": prog}
            ).scalar()
            is None
        )


def test_annotate_alias_y_nota_acumulan(capsys: pytest.CaptureFixture[str]) -> None:
    pid = _mk("persona", "Ada")

    assert main(["annotate", "--id", str(pid), "--alias", "Ada L", "--note", "mi tutora"]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "annotate",
                "--id",
                str(pid),
                "--alias",
                "Ada L",
                "--alias",
                "Ada",
                "--note",
                "vive en Bogotá",
                "--json",
            ]
        )
        == 0
    )
    row = _last_json(capsys.readouterr().out)
    assert row["aliases"] == ["Ada L"]  # dedup + nunca el display_name
    assert row["notes"] == "mi tutora\nvive en Bogotá"  # las notas se anexan

    assert main(["annotate", "--id", str(pid)]) == 1  # sin --alias ni --note
    assert "Nada para anotar" in capsys.readouterr().err
    assert main(["annotate", "--id", "99999", "--note", "x"]) == 1


# ----- resolve -------------------------------------------------------------------- #


def test_resolve_distinct_rechaza_y_quedan_ambas(capsys: pytest.CaptureFixture[str]) -> None:
    a = _mk("persona", "Ana Pérez")
    b = _mk("persona", "Ana Gómez")
    cand = _mk_candidate(a, b)

    assert (
        main(["resolve", "--candidate", str(cand), "--distinct", "--why", "homónimas", "--json"])
        == 0
    )
    out = _last_json(capsys.readouterr().out)
    assert out["decision"] == "distinct"
    with connection() as c:
        row = c.execute(
            text(
                "SELECT status, decided_by, rationale "
                "FROM mod_identidades_merge_candidates WHERE id = :i"
            ),
            {"i": cand},
        ).first()
        n = c.execute(text("SELECT count(*) FROM mod_identidades WHERE user_id = 1")).scalar_one()
    assert row is not None and (row[0], row[1], row[2]) == ("rejected", "agent", "homónimas")
    assert n == 2  # coexisten

    # ya decidido → error claro
    assert main(["resolve", "--candidate", str(cand), "--same"]) == 1
    assert "ya fue decidido" in capsys.readouterr().err


def test_resolve_same_fusiona(capsys: pytest.CaptureFixture[str]) -> None:
    a = _mk("persona", "Roy M")
    b = _mk("persona", "Roy Monroy")
    surv, absb = min(a, b), max(a, b)
    cand = _mk_candidate(a, b)

    assert (
        main(
            [
                "resolve",
                "--candidate",
                str(cand),
                "--same",
                "--why",
                "lo confirmó el usuario",
                "--json",
            ]
        )
        == 0
    )
    out = _last_json(capsys.readouterr().out)
    assert out["decision"] == "same" and out["survivor"]["id"] == surv
    with connection() as c:
        ids = [
            int(r[0])
            for r in c.execute(text("SELECT id FROM mod_identidades WHERE user_id = 1")).all()
        ]
        cand_left = c.execute(
            text("SELECT count(*) FROM mod_identidades_merge_candidates WHERE id = :i"),
            {"i": cand},
        ).scalar_one()
        aliases = c.execute(
            text("SELECT aliases FROM mod_identidades WHERE id = :i"), {"i": surv}
        ).scalar_one()
    assert ids == [surv]  # la absorbida desapareció
    assert cand_left == 0  # el candidato cayó por FK CASCADE
    absorbed_name = "Roy Monroy" if absb == b else "Roy M"
    assert absorbed_name in aliases  # el nombre absorbido quedó como alias


def test_resolve_candidato_inexistente(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["resolve", "--candidate", "99999", "--same"]) == 1
    assert "No existe el candidato" in capsys.readouterr().err
