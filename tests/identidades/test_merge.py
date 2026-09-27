"""`merge_identities`: funde dos identidades canónicas — mueve identificadores/afiliaciones,
re-apunta menciones, aristas del grafo (colapsando dup lógico/self-loop) y membresías de cúmulo,
suma alias, deja auditoría y borra la absorbida. Contra la DB real."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.merge import (
    drop_colliding_aliases,
    merge_identities,
    split_identity,
)
from memex.relations.cluster_store import materialize_cluster_edges
from memex.relations.maintenance import reconcile_graph


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


def test_merge_ancestro_en_descendiente_no_deja_ciclo(conn: Any) -> None:
    # H2: fundir un ANCESTRO dentro de un DESCENDIENTE. Cadena surv → mid → absb (surv = NIETO de
    # absb). El re-apunte 4b cuelga `mid` del superviviente mientras el fill-only mantiene surv→mid
    # → 2-ciclo (el CHECK de la DB solo atrapa el self-loop directo). El guard anti-ciclo
    # (`would_create_cycle`) lo rompe anulando el padre del superviviente.
    absb = _mk_person(conn, "Universidad")  # ancestro absorbido
    mid = _mk_person(conn, "Facultad")  # nodo intermedio
    surv = _mk_person(conn, "Programa")  # descendiente superviviente
    _set_parent(conn, mid, absb)  # Facultad → Universidad
    _set_parent(conn, surv, mid)  # Programa → Facultad
    assert merge_identities(conn, 1, surv, absb) is True
    assert _parent_of(conn, mid) == surv  # el intermedio cuelga del superviviente
    assert _parent_of(conn, surv) is None  # y NO quedó ciclo: su padre se anuló
    # sanity: ningún par mutuamente apuntado (a→b y b→a)
    cycles = conn.execute(
        text(
            "SELECT a.id FROM mod_identidades a JOIN mod_identidades b "
            "ON a.parent_identity_id = b.id AND b.parent_identity_id = a.id WHERE a.user_id = 1"
        )
    ).all()
    assert cycles == []


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
    surv = _mk_person(conn, "Danilo")
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


def test_merge_membresia_sin_churn_en_mantenimiento(conn: Any) -> None:
    # regresión del churn observado en dev: la membresía huérfana de la absorbida hacía que cada
    # corrida re-creara su arista `miembro_de` (a un vértice muerto) y la podara como huérfana. La
    # materialización (fase de cúmulos) crea solo la del superviviente; reconcile no halla huérfana.
    surv = _mk_person(conn, "Danilo")
    absb = _mk_person(conn, "CoKeFish")
    cid = _mk_cluster(conn, "Blizzard", "6" * 64)
    _add_member(conn, cid, absb)
    assert merge_identities(conn, 1, surv, absb) is True
    cluster_edges = materialize_cluster_edges(conn, 1)
    rstats = reconcile_graph(conn, 1)
    assert rstats.orphans_pruned == 0
    assert cluster_edges == 1  # la miembro_de del superviviente, viva
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


def _mk_producto(conn: Any, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'producto',:n) RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def test_merge_producto_repunta_con_slug_producto(conn: Any) -> None:
    # producto con producto fusiona y re-apunta aristas + membresía con el slug
    # 'identidades:producto' (mapa IDENTITY_SLUG_BY_KIND; antes era KeyError).
    surv = _mk_producto(conn, "Claude")
    absb = _mk_producto(conn, "ClaudeAI")
    conn.execute(
        text(
            "INSERT INTO relation_edges (user_id, src_slug, src_id, dst_slug, dst_id, producer) "
            "VALUES (1,'identidades:producto',:a,'finance',99,'inbox')"
        ),
        {"a": absb},
    )
    cluster = _mk_cluster(conn, "IA", "6" * 64)
    conn.execute(
        text(
            "INSERT INTO relation_cluster_members (user_id, cluster_id, member_slug, member_id) "
            "VALUES (1,:c,'identidades:producto',:a)"
        ),
        {"c": cluster, "a": absb},
    )
    assert merge_identities(conn, 1, surv, absb) is True
    assert conn.execute(text("SELECT src_id FROM relation_edges")).scalar_one() == surv
    assert (
        conn.execute(
            text("SELECT member_id FROM relation_cluster_members WHERE cluster_id = :c"),
            {"c": cluster},
        ).scalar_one()
        == surv
    )


def test_merge_desconocido_repunta_con_slug_desconocido(conn: Any) -> None:
    # desconocido con desconocido fusiona y re-apunta aristas con el slug 'identidades:desconocido'
    # (mapa IDENTITY_SLUG_BY_KIND; sin la entrada sería KeyError en merge.py).
    surv = int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'desconocido','Buzon A') RETURNING id"
            )
        ).scalar_one()
    )
    absb = int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'desconocido','Buzon B') RETURNING id"
            )
        ).scalar_one()
    )
    conn.execute(
        text(
            "INSERT INTO relation_edges (user_id, src_slug, src_id, dst_slug, dst_id, producer) "
            "VALUES (1,'identidades:desconocido',:a,'finance',99,'inbox')"
        ),
        {"a": absb},
    )
    assert merge_identities(conn, 1, surv, absb) is True
    assert conn.execute(text("SELECT src_id FROM relation_edges")).scalar_one() == surv


def _mk_desc(conn: Any, name: str) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1,'desconocido',:n) RETURNING id"
            ),
            {"n": name},
        ).scalar_one()
    )


def test_merge_cross_kind_typed_funde(conn: Any) -> None:
    # cross-kind entre tipos definidos: misma entidad tipada distinto (Notion org/producto) ahora SÍ
    # funde (experimento «merges entre tipos»). Gana el `keep_id` (la org); el producto se absorbe.
    org = _mk_org(conn, "Notion")
    prod = _mk_producto(conn, "Notion")
    assert merge_identities(conn, 1, org, prod) is True
    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :p"), {"p": prod}
        ).scalar_one()
        == 0
    )
    assert (
        conn.execute(
            text("SELECT kind FROM mod_identidades WHERE id = :o"), {"o": org}
        ).scalar_one()
        == "organizacion"
    )


def test_merge_cross_kind_desconocido_into_org(conn: Any) -> None:
    # caso motivador: una `desconocido` (buzón con email + afiliación a la org, como deja el camino
    # del remitente) se funde en la org. El email cuelga de la org, sin self-afiliación ni arista
    # huérfana del slug `identidades:desconocido`.
    org = _mk_org(conn, "tusclases.co")
    desc = _mk_desc(conn, "info@tusclases.co")
    conn.execute(
        text(
            "INSERT INTO mod_identidades_identifiers "
            "(user_id, identity_id, platform, kind, value, value_norm) "
            "VALUES (1,:i,'email','email','info@tusclases.co','info@tusclases.co')"
        ),
        {"i": desc},
    )
    conn.execute(
        text(
            "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id) VALUES (1,:p,:o)"
        ),
        {"p": desc, "o": org},
    )
    conn.execute(
        text(
            "INSERT INTO relation_edges (user_id, src_slug, src_id, dst_slug, dst_id, producer) "
            "VALUES (1,'identidades:desconocido',:a,'finance',99,'inbox')"
        ),
        {"a": desc},
    )
    assert merge_identities(conn, 1, org, desc) is True
    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :a"), {"a": desc}
        ).scalar_one()
        == 0
    )
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM mod_identidades_identifiers "
                "WHERE identity_id = :o AND value_norm = 'info@tusclases.co'"
            ),
            {"o": org},
        ).scalar_one()
        == 1
    )
    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM mod_identidades_person_orgs "
                "WHERE person_id = :o AND org_id = :o"
            ),
            {"o": org},
        ).scalar_one()
        == 0
    )
    assert (
        conn.execute(
            text("SELECT count(*) FROM relation_edges WHERE src_slug = 'identidades:desconocido'")
        ).scalar_one()
        == 0
    )


def test_merge_cross_kind_desconocido_survivor_swaps(conn: Any) -> None:
    # si el caller propone como superviviente la `desconocido`, se INVIERTE: gana el tipo definido.
    org = _mk_org(conn, "Acme")
    desc = _mk_desc(conn, "Acme (sin tipar)")
    assert merge_identities(conn, 1, desc, org) is True  # survivor=desc, absorbed=org → se invierte
    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :d"), {"d": desc}
        ).scalar_one()
        == 0
    )  # la `desconocido` es la que se absorbió
    surv = conn.execute(
        text("SELECT kind FROM mod_identidades WHERE id = :o"), {"o": org}
    ).scalar_one()
    assert surv == "organizacion"  # sobrevivió la org tipada
    aliases = conn.execute(
        text("SELECT aliases FROM mod_identidades WHERE id = :o"), {"o": org}
    ).scalar_one()
    assert "Acme (sin tipar)" in aliases  # el nombre de la desconocido quedó como alias


def test_merge_marca_dirty_superviviente_y_vecino(conn: Any) -> None:
    # integración (vía graph_writer.merge_vertices): el merge marca dirty al superviviente y al
    # ex-vecino del absorbido (groundwork ADR-021) — antes el merge no avisaba al grafo.
    surv = _mk_person(conn, "Ada")
    absb = _mk_person(conn, "Ada L.")
    conn.execute(
        text(
            "INSERT INTO relation_edges (user_id, src_slug, src_id, dst_slug, dst_id, producer) "
            "VALUES (1,'identidades:person',:a,'finance',99,'inbox')"
        ),
        {"a": absb},
    )
    assert merge_identities(conn, 1, surv, absb) is True
    dirty = {
        (str(r[0]), int(r[1]))
        for r in conn.execute(
            text("SELECT slug, id FROM relation_vertex_state WHERE user_id = 1 AND dirty")
        ).all()
    }
    assert ("identidades:person", surv) in dirty  # el superviviente
    assert ("finance", 99) in dirty  # el ex-vecino del absorbido


def test_split_deshace_una_fusion_por_nombre(conn: Any) -> None:
    """Tras fundir «RAS Salesiana IEEE» en la universidad, `split_identity` la vuelve a crear desde
    el rastro: quita el alias, re-apunta sus menciones (por nombre) y mueve el identificador
    (email) que esas menciones traían, junto con las menciones de remitente de ese email."""
    uni = _mk_org(conn, "Pontificia Universidad Salesiana")
    ras = _mk_org(conn, "RAS Salesiana IEEE")
    conn.execute(
        text(
            "INSERT INTO mod_identidades_identifiers (user_id, identity_id, platform, kind, value, "
            "value_norm) VALUES (1, :i, 'email', 'email', 'ras@salesiana.edu.co', "
            "'ras@salesiana.edu.co'), (1, :u, 'email', 'email', 'info@salesiana.edu.co', "
            "'info@salesiana.edu.co')"
        ),
        {"i": ras, "u": uni},
    )
    for name, email, ident in (
        ("RAS Salesiana IEEE", "ras@salesiana.edu.co", ras),
        ("ras salesiana ieee", None, ras),
        ("Pontificia Universidad Salesiana", "info@salesiana.edu.co", uni),
    ):
        conn.execute(
            text(
                "INSERT INTO mod_identidades_mentions (user_id, source_inbox_ids, evidence, "
                "mentioned_name, mentioned_kind, email, confidence, resolved_kind, "
                "resolved_identity_id, resolution_method) VALUES (1, ARRAY[1]::bigint[], 'x', :n, "
                "'organizacion', :e, 0.9, 'organizacion', :i, 'exact_name')"
            ),
            {"n": name, "e": email, "i": ident},
        )
    assert merge_identities(conn, 1, uni, ras)
    aliases = conn.execute(
        text("SELECT aliases FROM mod_identidades WHERE id = :u"), {"u": uni}
    ).scalar_one()
    assert "RAS Salesiana IEEE" in list(aliases)

    new_id = split_identity(conn, 1, uni, "RAS Salesiana IEEE")
    assert new_id is not None and new_id != ras
    row = conn.execute(
        text(
            "SELECT kind, display_name, source, metadata->>'split_from' FROM mod_identidades "
            "WHERE id = :n"
        ),
        {"n": new_id},
    ).one()
    assert tuple(row) == ("organizacion", "RAS Salesiana IEEE", "manual", str(uni))
    aliases = conn.execute(
        text("SELECT aliases FROM mod_identidades WHERE id = :u"), {"u": uni}
    ).scalar_one()
    assert "RAS Salesiana IEEE" not in list(aliases)
    mentions = conn.execute(
        text(
            "SELECT resolved_identity_id, count(*) FROM mod_identidades_mentions WHERE user_id = 1 "
            "GROUP BY 1 ORDER BY 1"
        )
    ).all()
    assert {(int(r[0]), int(r[1])) for r in mentions} == {(uni, 1), (new_id, 2)}
    ids = conn.execute(
        text(
            "SELECT identity_id, value FROM mod_identidades_identifiers WHERE user_id = 1 "
            "ORDER BY 2"
        )
    ).all()
    assert [(int(r[0]), r[1]) for r in ids] == [
        (uni, "info@salesiana.edu.co"),
        (new_id, "ras@salesiana.edu.co"),
    ]
    assert split_identity(conn, 1, 999_999, "X") is None
    assert split_identity(conn, 1, uni, "  ") is None


def test_alias_que_es_nombre_de_otra_ficha_viva_se_quita(conn: Any) -> None:
    """Integridad: si la superviviente queda con un alias igual al nombre canónico de otra ficha
    viva (fusiones viejas dejaban «Carrera de Ingeniería Electrónica» como alias de la
    universidad), la ficha propia gana y el alias se quita — en el merge y en el barrido."""
    uni = _mk_org(conn, "Pontificia Universidad Salesiana")
    dup = _mk_org(conn, "Salesiana")
    carrera = _mk_org(conn, "Carrera de Ingeniería Electrónica")
    conn.execute(
        text(
            "UPDATE mod_identidades SET aliases = ARRAY['Carrera de Ingeniería Electrónica'] "
            "WHERE id = :d"
        ),
        {"d": dup},
    )
    assert merge_identities(conn, 1, uni, dup)
    aliases = conn.execute(
        text("SELECT aliases FROM mod_identidades WHERE id = :u"), {"u": uni}
    ).scalar_one()
    # «Salesiana» (la absorbida) queda; el alias que colisiona con la carrera viva NO.
    assert "Salesiana" in list(aliases) and "Carrera de Ingeniería Electrónica" not in list(aliases)
    assert (
        conn.execute(
            text("SELECT count(*) FROM mod_identidades WHERE id = :c"), {"c": carrera}
        ).scalar_one()
        == 1
    )
    # Barrido global: una colisión preexistente también se limpia.
    conn.execute(
        text(
            "UPDATE mod_identidades SET aliases = aliases || "
            "ARRAY['carrera de ingenieria electronica'] WHERE id = :u"
        ),
        {"u": uni},
    )
    dropped = drop_colliding_aliases(conn, 1)
    assert dropped == [(uni, "carrera de ingenieria electronica", carrera)]
    assert drop_colliding_aliases(conn, 1) == []
