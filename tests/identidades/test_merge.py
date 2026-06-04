"""`merge_identities`: funde dos identidades canónicas — mueve identificadores/afiliaciones,
re-apunta menciones y aristas del grafo (colapsando dup lógico/self-loop), suma alias, deja
auditoría y borra la absorbida. Contra la DB real."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.merge import merge_identities


def _mk_person(conn: Any, name: str, email: str | None = None) -> int:
    iid = int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'persona',:n) RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )
    if email:
        conn.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm) "
                "VALUES (1,:i,'email','email',:e,:e)"
            ),
            {"i": iid, "e": email},
        )
    return iid


def test_merge_moves_and_repoints(conn: Any) -> None:
    surv = _mk_person(conn, "Ada Lovelace", "ada@x.com")
    absb = _mk_person(conn, "Ada L.", "ada@work.com")
    conn.execute(
        text(
            "INSERT INTO mod_identidades_mentions "
            "(user_id, source_inbox_ids, mentioned_name, resolved_kind, resolved_identity_id, "
            " resolution_method) VALUES (1, ARRAY[5], 'Ada L.', 'persona', :a, 'created')"
        ),
        {"a": absb},
    )
    conn.execute(
        text(
            "INSERT INTO relation_edges (user_id, src_slug, src_id, dst_slug, dst_id, producer) "
            "VALUES (1,'identidades:person',:a,'finance',99,'inbox')"
        ),
        {"a": absb},
    )

    assert merge_identities(conn, 1, surv, absb) is True

    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :a"), {"a": absb}
        ).scalar_one()
        == 0
    )
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM mod_identidades_identifiers "
                "WHERE identity_id = :s AND value_norm = 'ada@work.com'"
            ),
            {"s": surv},
        ).scalar_one()
        == 1
    )
    aliases = conn.execute(
        text("SELECT aliases FROM mod_identidades WHERE id = :s"), {"s": surv}
    ).scalar_one()
    assert "Ada L." in aliases
    assert (
        conn.execute(text("SELECT resolved_identity_id FROM mod_identidades_mentions")).scalar_one()
        == surv
    )
    assert conn.execute(text("SELECT src_id FROM relation_edges")).scalar_one() == surv
    merged_from = conn.execute(
        text("SELECT metadata->'merged_from' FROM mod_identidades WHERE id = :s"), {"s": surv}
    ).scalar_one()
    assert merged_from == [absb]


def test_merge_collapses_self_loop(conn: Any) -> None:
    surv = _mk_person(conn, "A")
    absb = _mk_person(conn, "B")
    # arista absorbida→superviviente (mismo slug): tras el merge sería un self-loop → se borra
    conn.execute(
        text(
            "INSERT INTO relation_edges (user_id, src_slug, src_id, dst_slug, dst_id, producer) "
            "VALUES (1,'identidades:person',:a,'identidades:person',:s,'inbox')"
        ),
        {"a": absb, "s": surv},
    )
    assert merge_identities(conn, 1, surv, absb) is True
    assert (
        conn.execute(text("SELECT count(*) FROM relation_edges WHERE src_id = dst_id")).scalar_one()
        == 0
    )


def test_merge_dedups_logical_edge(conn: Any) -> None:
    surv = _mk_person(conn, "A")
    absb = _mk_person(conn, "B")
    # ambas tienen la MISMA arista lógica (mismo dst/type/producer) → tras re-apuntar colapsa a una
    for who in (surv, absb):
        conn.execute(
            text(
                "INSERT INTO relation_edges "
                "(user_id, src_slug, src_id, dst_slug, dst_id, relation_type, producer) "
                "VALUES (1,'identidades:person',:x,'finance',7,'','inbox')"
            ),
            {"x": who},
        )
    assert merge_identities(conn, 1, surv, absb) is True
    assert (
        conn.execute(
            text("SELECT count(*) FROM relation_edges WHERE src_id = :s"), {"s": surv}
        ).scalar_one()
        == 1
    )
