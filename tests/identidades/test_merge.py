"""`merge_identities`: funde dos identidades canónicas — mueve identificadores/afiliaciones,
re-apunta menciones, aristas del grafo (colapsando dup lógico/self-loop) y membresías de cúmulo,
suma alias, deja auditoría y borra la absorbida. Contra la DB real."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.merge import merge_identities
from memex.relations.deterministic import build_relations


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


def _set_parent(conn: Any, child: int, parent: int | None) -> None:
    conn.execute(
        text("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c"),
        {"p": parent, "c": child},
    )


def _parent_of(conn: Any, identity_id: int) -> int | None:
    row = conn.execute(
        text("SELECT parent_identity_id FROM mod_identidades WHERE id = :i"), {"i": identity_id}
    ).scalar()
    return int(row) if row is not None else None


def test_merge_reparents_children(conn: Any) -> None:
    surv = _mk_person(conn, "A")
    absb = _mk_person(conn, "B")
    child = _mk_person(conn, "Hijo")
    _set_parent(conn, child, absb)
    assert merge_identities(conn, 1, surv, absb) is True
    assert _parent_of(conn, child) == surv  # el hijo del absorbido cuelga del superviviente


def test_merge_inherits_parent_fill_only(conn: Any) -> None:
    surv = _mk_person(conn, "A")  # sin padre
    absb = _mk_person(conn, "B")
    grandparent = _mk_person(conn, "P")
    _set_parent(conn, absb, grandparent)
    assert merge_identities(conn, 1, surv, absb) is True
    assert _parent_of(conn, surv) == grandparent  # hereda el padre del absorbido (fill-only)


def test_merge_child_and_parent_no_self_parent(conn: Any) -> None:
    # superviviente colgaba del absorbido → tras fundirlos NO debe quedar self-parent.
    surv = _mk_person(conn, "A")
    absb = _mk_person(conn, "B")
    _set_parent(conn, surv, absb)
    assert merge_identities(conn, 1, surv, absb) is True
    assert _parent_of(conn, surv) is None


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


def _mk_org(conn: Any, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'organizacion',:n) RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def _mk_cluster(conn: Any, name: str, sig: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO relation_clusters "
                "(user_id, status, name, signature, blob_signature, member_count) "
                "VALUES (1,'confirmed',:n,:s,:s,2) RETURNING id"
            ),
            {"n": name, "s": sig},
        ).scalar_one()
    )


def _add_member(conn: Any, cluster_id: int, member_id: int, *, pruned: bool = False) -> None:
    conn.execute(
        text(
            "INSERT INTO relation_cluster_members "
            "(user_id, cluster_id, member_slug, member_id, pruned) "
            "VALUES (1,:c,'identidades:person',:m,:p)"
        ),
        {"c": cluster_id, "m": member_id, "p": pruned},
    )


def test_merge_repunta_membresia_de_cumulo(conn: Any) -> None:
    # la absorbida era miembro de un cúmulo y la superviviente no → la membresía se re-apunta.
    surv = _mk_person(conn, "Ada")
    absb = _mk_person(conn, "Ada L.")
    cid = _mk_cluster(conn, "Contexto", "4" * 64)
    _add_member(conn, cid, absb)
    assert merge_identities(conn, 1, surv, absb) is True
    members = (
        conn.execute(
            text("SELECT member_id FROM relation_cluster_members WHERE cluster_id = :c"),
            {"c": cid},
        )
        .scalars()
        .all()
    )
    assert list(members) == [surv]


def test_merge_membresia_duplicada_gana_superviviente(conn: Any) -> None:
    # ambas eran miembros del MISMO cúmulo: la fila de la absorbida se borra (la UNIQUE impediría
    # el re-apuntado) y queda la del superviviente con su estado, incluido `pruned`.
    surv = _mk_person(conn, "Rodion")
    absb = _mk_person(conn, "CoKeFish")
    cid = _mk_cluster(conn, "Blizzard", "5" * 64)
    _add_member(conn, cid, surv, pruned=True)
    _add_member(conn, cid, absb)
    assert merge_identities(conn, 1, surv, absb) is True
    rows = conn.execute(
        text("SELECT member_id, pruned FROM relation_cluster_members WHERE cluster_id = :c"),
        {"c": cid},
    ).all()
    assert [(int(r[0]), bool(r[1])) for r in rows] == [(surv, True)]


def test_merge_membresia_sin_churn_en_build(conn: Any) -> None:
    # regresión del churn observado en dev: la membresía huérfana de la absorbida hacía que cada
    # build re-creara su arista `miembro_de` (a un vértice muerto) y la podara como huérfana.
    surv = _mk_person(conn, "Rodion")
    absb = _mk_person(conn, "CoKeFish")
    cid = _mk_cluster(conn, "Blizzard", "6" * 64)
    _add_member(conn, cid, absb)
    assert merge_identities(conn, 1, surv, absb) is True
    stats = build_relations(conn, 1)
    assert stats.orphans_pruned == 0
    assert stats.cluster_edges == 1  # la miembro_de del superviviente, viva
    pair = conn.execute(
        text(
            "SELECT src_slug, src_id FROM relation_edges "
            "WHERE user_id = 1 AND relation_type = 'miembro_de'"
        )
    ).all()
    assert [(r[0], int(r[1])) for r in pair] == [("identidades:person", surv)]


def test_merge_repunta_counterparty_de_finanzas(conn: Any) -> None:
    # fusionar dos orgs re-apunta counterparty_identity_id de finanzas (consolidado + cruda) al
    # superviviente; si no, el FK ON DELETE SET NULL lo dejaría NULL y se perdería el vínculo.
    surv = _mk_org(conn, "Acme Inc")
    absb = _mk_org(conn, "Acme")
    cid = int(
        conn.execute(
            text(
                "INSERT INTO mod_finance_consolidated "
                "(user_id, direction, amount, currency, occurred_at, counterparty, "
                " counterparty_identity_id) "
                "VALUES (1,'egreso',100,'COP',NOW(),'Acme',:a) RETURNING id"
            ),
            {"a": absb},
        ).scalar_one()
    )
    tid = int(
        conn.execute(
            text(
                "INSERT INTO mod_finance_transactions "
                "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, "
                " counterparty, counterparty_identity_id) "
                "VALUES (1, ARRAY[7], 'egreso',100,'COP',NOW(),'Acme',:a) RETURNING id"
            ),
            {"a": absb},
        ).scalar_one()
    )
    assert merge_identities(conn, 1, surv, absb) is True
    assert (
        conn.execute(
            text("SELECT counterparty_identity_id FROM mod_finance_consolidated WHERE id = :c"),
            {"c": cid},
        ).scalar_one()
        == surv
    )
    assert (
        conn.execute(
            text("SELECT counterparty_identity_id FROM mod_finance_transactions WHERE id = :t"),
            {"t": tid},
        ).scalar_one()
        == surv
    )
