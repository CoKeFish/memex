"""`vertex_health`: detección read-only de entidades sospechosas del directorio."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.modules.identidades.health import vertex_health


def _id(conn: Any, kind: str, name: str, *, source: str = "extraction") -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, source) "
                "VALUES (1, :k, :n, :s) RETURNING id"
            ),
            {"k": kind, "n": name, "s": source},
        ).scalar_one()
    )


def _mention(conn: Any, identity_id: int, inbox_id: int = 5) -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades_mentions "
            "(user_id, source_inbox_ids, mentioned_name, resolved_identity_id, resolution_method) "
            "VALUES (1, ARRAY[:ib], 'x', :i, 'created')"
        ),
        {"ib": inbox_id, "i": identity_id},
    )


def _idf(
    conn: Any, identity_id: int, value: str, kind: str = "email", platform: str = "email"
) -> None:
    conn.execute(
        text(
            "INSERT INTO mod_identidades_identifiers "
            "(user_id, identity_id, platform, kind, value, value_norm) "
            "VALUES (1, :i, :p, :k, :v, :v)"
        ),
        {"i": identity_id, "p": platform, "k": kind, "v": value},
    )


def test_orphan_detectada(conn: Any) -> None:
    huerf = _id(conn, "organizacion", "Huérfana")  # sin mención / hijos / afiliación
    viva = _id(conn, "organizacion", "Viva")
    _mention(conn, viva)
    rep = vertex_health(conn, 1)
    orphan_ids = {e.id for e in rep.orphans}
    assert huerf in orphan_ids
    assert viva not in orphan_ids


def test_shared_identifier_detectado(conn: Any) -> None:
    # un identificador FUERTE (email/phone/domain) ya NO puede compartirse (índice 0081). El check
    # sigue cazando los que SÍ pueden: un handle por-plataforma reclamado por dos identidades.
    a = _id(conn, "persona", "A")
    b = _id(conn, "persona", "B")
    _idf(conn, a, "foo", kind="handle", platform="twitter")
    _idf(conn, b, "foo", kind="handle", platform="twitter")  # mismo handle en dos identidades
    _mention(conn, a)
    _mention(conn, b)
    rep = vertex_health(conn, 1)
    assert any(set(s.ids) == {a, b} and s.key == "foo" for s in rep.shared_identifiers)


def test_cross_kind_homonym_detectado(conn: Any) -> None:
    p = _id(conn, "persona", "Claude")
    pr = _id(conn, "producto", "Claude")
    _mention(conn, p)
    _mention(conn, pr)
    rep = vertex_health(conn, 1)
    assert any(set(s.ids) == {p, pr} for s in rep.cross_kind_homonyms)


def test_containment_dup_detectado(conn: Any) -> None:
    corto = _id(conn, "persona", "Jose David")
    largo = _id(conn, "persona", "Jose David Reyes Sanchez")
    _mention(conn, corto)
    _mention(conn, largo)
    rep = vertex_health(conn, 1)
    assert any({p.a_id, p.b_id} == {corto, largo} for p in rep.containment_dups)


def test_empty_org_core_detectado(conn: Any) -> None:
    g = _id(conn, "organizacion", "Group")  # org_core = '' (puro sufijo)
    _mention(conn, g)
    rep = vertex_health(conn, 1)
    assert g in {e.id for e in rep.empty_org_core}


def test_cycle_detectado(conn: Any) -> None:
    # A→B y B→A: el CHECK no_self_parent NO lo impide (ninguno es self), pero es un 2-ciclo.
    a = _id(conn, "organizacion", "A")
    b = _id(conn, "organizacion", "B")
    conn.execute(
        text("UPDATE mod_identidades SET parent_identity_id = :b WHERE id = :a"), {"a": a, "b": b}
    )
    conn.execute(
        text("UPDATE mod_identidades SET parent_identity_id = :a WHERE id = :b"), {"a": a, "b": b}
    )
    rep = vertex_health(conn, 1)
    assert a in rep.cycles and b in rep.cycles


def test_directorio_sano_sin_hallazgos(conn: Any) -> None:
    x = _id(conn, "organizacion", "Acme")
    _mention(conn, x)
    rep = vertex_health(conn, 1)
    assert rep.total == 1
    assert rep.by_kind == {"organizacion": 1}
    assert rep.suspicious == 0


def test_pending_classification_lista_desconocidos(conn: Any) -> None:
    # las entidades `desconocido` salen en pending_classification (backlog de set-kind). Con mención
    # NO son huérfanas y NO inflan `suspicious` (es un estado esperado, no una anomalía a corregir).
    d1 = _id(conn, "desconocido", "ielec")
    d2 = _id(conn, "desconocido", "viceacad")
    p = _id(conn, "persona", "Ana")
    _mention(conn, d1)
    _mention(conn, d2)
    _mention(conn, p)
    rep = vertex_health(conn, 1)
    assert {e.id for e in rep.pending_classification} == {d1, d2}
    assert all(e.kind == "desconocido" for e in rep.pending_classification)
    assert rep.suspicious == 0  # el backlog de clasificación no cuenta como hallazgo anómalo
