"""Backfill org→producto por voto de menciones: la mayoría ESTRICTA reclasifica (kind +
resolved_kind + re-slug de aristas y membresías + candidatos de merge cross-kind rechazados);
empate/minoría/sin-menciones conserva; scoped por kind y por user; el CLI es dry-run por default
(no escribe) y `--apply` es idempotente."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.backfill import apply_reclassification, find_product_candidates
from memex.modules.identidades.cli import main


def _identity(conn: Any, kind: str, name: str, user_id: int = 1) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (:u, :k, :n) RETURNING id"
            ),
            {"u": user_id, "k": kind, "n": name},
        ).scalar_one()
    )


def _mention(conn: Any, identity_id: int, mentioned_kind: str, user_id: int = 1) -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades_mentions "
            "(user_id, source_inbox_ids, mentioned_name, mentioned_kind, resolved_kind, "
            " resolved_identity_id) VALUES (:u, ARRAY[1], 'X', :mk, 'organizacion', :i)"
        ),
        {"u": user_id, "mk": mentioned_kind, "i": identity_id},
    )


def _kind_of(conn: Any, identity_id: int) -> str:
    return str(
        conn.execute(
            text("SELECT kind FROM mod_identidades WHERE id = :i"), {"i": identity_id}
        ).scalar_one()
    )


def test_mayoria_estricta_reclasifica_y_reapunta(conn: Any) -> None:
    steam = _identity(conn, "organizacion", "Steam")
    for mk in ("producto", "producto", "organizacion"):
        _mention(conn, steam, mk)
    # aristas en ambos extremos + membresía de cúmulo con el slug viejo
    conn.execute(
        text(
            "INSERT INTO relation_edges (user_id, src_slug, src_id, dst_slug, dst_id, producer) "
            "VALUES (1,'identidades:org',:i,'finance',99,'inbox'),"
            "       (1,'finance',98,'identidades:org',:i,'finance')"
        ),
        {"i": steam},
    )
    cluster = int(
        conn.execute(
            text(
                "INSERT INTO relation_clusters "
                "(user_id, status, name, signature, blob_signature, member_count) "
                "VALUES (1,'confirmed','Juegos',:s,:s,2) RETURNING id"
            ),
            {"s": "7" * 64},
        ).scalar_one()
    )
    conn.execute(
        text(
            "INSERT INTO relation_cluster_members (user_id, cluster_id, member_slug, member_id) "
            "VALUES (1,:c,'identidades:org',:i)"
        ),
        {"c": cluster, "i": steam},
    )

    cands = find_product_candidates(conn, 1)
    assert [(c.id, c.votos_producto, c.votos_total) for c in cands] == [(steam, 2, 3)]

    stats = apply_reclassification(conn, 1, [c.id for c in cands])
    assert stats.reclassified == 1
    assert _kind_of(conn, steam) == "producto"
    rk = (
        conn.execute(
            text(
                "SELECT DISTINCT resolved_kind FROM mod_identidades_mentions "
                "WHERE resolved_identity_id = :i"
            ),
            {"i": steam},
        )
        .scalars()
        .all()
    )
    assert rk == ["producto"]
    slugs = conn.execute(text("SELECT src_slug, dst_slug FROM relation_edges ORDER BY id")).all()
    assert [tuple(r) for r in slugs] == [
        ("identidades:producto", "finance"),
        ("finance", "identidades:producto"),
    ]
    member_slug = conn.execute(
        text("SELECT member_slug FROM relation_cluster_members WHERE cluster_id = :c"),
        {"c": cluster},
    ).scalar_one()
    assert member_slug == "identidades:producto"
    assert stats.edges == 2 and stats.cluster_members == 1


def test_empate_minoria_y_sin_menciones_conservan(conn: Any) -> None:
    empate = _identity(conn, "organizacion", "Kentucky")
    _mention(conn, empate, "producto")
    _mention(conn, empate, "organizacion")
    minoria = _identity(conn, "organizacion", "Argentina")
    _mention(conn, minoria, "organizacion")
    _mention(conn, minoria, "organizacion")
    _mention(conn, minoria, "producto")
    _identity(conn, "organizacion", "SinMenciones")
    assert find_product_candidates(conn, 1) == []


def test_solo_orgs_entran_al_voto(conn: Any) -> None:
    # personas y productos existentes quedan fuera aunque sus menciones digan producto
    p = _identity(conn, "persona", "Juan")
    _mention(conn, p, "producto")
    prod = _identity(conn, "producto", "Claude")
    _mention(conn, prod, "producto")
    assert find_product_candidates(conn, 1) == []


def test_scoping_por_user(conn: Any) -> None:
    conn.execute(text("INSERT INTO users (id, email, display_name) VALUES (2, 'u2@local', 'u2')"))
    ajeno = _identity(conn, "organizacion", "Steam", user_id=2)
    _mention(conn, ajeno, "producto", user_id=2)
    assert find_product_candidates(conn, 1) == []
    assert [c.id for c in find_product_candidates(conn, 2)] == [ajeno]


def test_candidato_merge_cross_kind_queda_rejected(conn: Any) -> None:
    steam = _identity(conn, "organizacion", "Steam")
    _mention(conn, steam, "producto")
    otra = _identity(conn, "organizacion", "Steamm")
    lo, hi = sorted([steam, otra])
    conn.execute(
        text(
            "INSERT INTO mod_identidades_merge_candidates "
            "(user_id, identity_a_id, identity_b_id, reason, score) "
            "VALUES (1, :a, :b, 'trgm_name', 0.8)"
        ),
        {"a": lo, "b": hi},
    )
    apply_reclassification(conn, 1, [steam])
    row = conn.execute(
        text("SELECT status, decided_by FROM mod_identidades_merge_candidates")
    ).one()
    assert (row[0], row[1]) == ("rejected", "backfill")


def test_candidato_merge_mismo_kind_sigue_pendiente(conn: Any) -> None:
    # si AMBOS lados se reclasifican, el par sigue siendo same-kind → queda pendiente (el
    # desempate LLM aún aplica)
    a = _identity(conn, "organizacion", "Claude")
    _mention(conn, a, "producto")
    b = _identity(conn, "organizacion", "ClaudeAI")
    _mention(conn, b, "producto")
    lo, hi = sorted([a, b])
    conn.execute(
        text(
            "INSERT INTO mod_identidades_merge_candidates "
            "(user_id, identity_a_id, identity_b_id, reason, score) "
            "VALUES (1, :a, :b, 'trgm_name', 0.8)"
        ),
        {"a": lo, "b": hi},
    )
    apply_reclassification(conn, 1, [a, b])
    assert _kind_of(conn, a) == _kind_of(conn, b) == "producto"
    status = conn.execute(text("SELECT status FROM mod_identidades_merge_candidates")).scalar_one()
    assert status == "candidate"


def test_apply_vacio_es_noop(conn: Any) -> None:
    stats = apply_reclassification(conn, 1, [])
    assert stats.reclassified == 0


# ----- CLI (dry-run gated; conexión propia, sembrar con connection()) --------------- #


def _seed_steam_via_connection() -> int:
    with connection() as c:
        steam = _identity(c, "organizacion", "Steam")
        _mention(c, steam, "producto")
        _mention(c, steam, "producto")
    return steam


def test_cli_dry_run_no_escribe(capsys: pytest.CaptureFixture[str]) -> None:
    steam = _seed_steam_via_connection()
    assert main(["backfill-productos"]) == 0
    out = capsys.readouterr().out
    assert "Steam" in out and "DRY-RUN" in out and "votos=2/2" in out
    with connection() as c:
        assert _kind_of(c, steam) == "organizacion"  # no escribió


def test_cli_apply_reclasifica_e_idempotente(capsys: pytest.CaptureFixture[str]) -> None:
    steam = _seed_steam_via_connection()
    assert main(["backfill-productos", "--apply"]) == 0
    assert "Reclasificadas 1" in capsys.readouterr().out
    with connection() as c:
        assert _kind_of(c, steam) == "producto"
    # segunda corrida: ya no hay candidatos (el voto solo mira orgs)
    assert main(["backfill-productos", "--apply"]) == 0
    assert "Sin candidatos" in capsys.readouterr().out
