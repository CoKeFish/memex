"""Proyección de vértices (Fase 1): las tablas `mod_*` se leen como una lista uniforme de vértices
`(slug, id, label, kind)`. calendar -> consolidado (sin borrados); identidades -> person/org; inbox
NO es vértice; scoping por usuario.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.relations.edges import Ref
from memex.relations.vertices import get_vertex, known_slugs, list_vertices


def _exec(sql: str, **params: Any) -> Any:
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def _seed_all(user_id: int = 1) -> dict[str, int]:
    fin = _exec(
        "INSERT INTO mod_finance_consolidated "
        "(user_id, direction, amount, currency, occurred_at, counterparty) "
        "VALUES (:u, 'egreso', 100, 'COP', NOW(), 'Rappi') RETURNING id",
        u=user_id,
    )
    hack = _exec(
        "INSERT INTO mod_hackathones_events (user_id, source_inbox_ids, name) "
        "VALUES (:u, ARRAY[5]::bigint[], 'HackBogota') RETURNING id",
        u=user_id,
    )
    cal = _exec(
        "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on) "
        "VALUES (:u, 'Reunión', DATE '2026-07-01') RETURNING id",
        u=user_id,
    )
    person = _exec(
        "INSERT INTO mod_identidades (user_id, kind, display_name) "
        "VALUES (:u, 'persona', 'Juan Valdez') RETURNING id",
        u=user_id,
    )
    org = _exec(
        "INSERT INTO mod_identidades (user_id, kind, display_name) "
        "VALUES (:u, 'organizacion', 'Acme') RETURNING id",
        u=user_id,
    )
    return {
        "finance": int(fin),
        "hackathones": int(hack),
        "calendar": int(cal),
        "identidades:person": int(person),
        "identidades:org": int(org),
    }


def test_proyecta_todos_los_tipos() -> None:
    ids = _seed_all()
    _exec(
        "INSERT INTO mod_bienestar_registros (user_id, category, activity, occurred_at) "
        "VALUES (1, 'comida', 'almuerzo', NOW())"
    )
    habito = int(
        _exec(
            "INSERT INTO mod_bienestar_habits (user_id, name, cadence, activity) "
            "VALUES (1, 'Gym', 'daily', 'gimnasio') RETURNING id"
        )
    )
    _exec(
        "INSERT INTO relation_clusters (user_id, status, name, signature, blob_signature, "
        "member_count) "
        "VALUES (1, 'confirmed', 'Mi contexto', :sig, :sig, 2)",
        sig="0" * 64,
    )
    with connection() as c:
        verts = list_vertices(c, 1)
    by_slug = {v.slug: v for v in verts}
    assert set(by_slug) == set(known_slugs())
    assert by_slug["finance"].kind == "transaccion"
    assert by_slug["finance"].label == "Rappi"
    assert by_slug["finance"].id == ids["finance"]
    assert by_slug["calendar"].label == "Reunión"
    assert by_slug["calendar"].kind == "evento"
    assert by_slug["identidades:person"].label == "Juan Valdez"
    assert by_slug["identidades:org"].kind == "organizacion"
    assert by_slug["bienestar"].kind == "registro"
    assert by_slug["bienestar"].label == "almuerzo"
    assert by_slug["bienestar:habito"].kind == "habito"
    assert by_slug["bienestar:habito"].label == "Gym"
    assert by_slug["bienestar:habito"].id == habito
    assert by_slug["cumulo"].kind == "cumulo"
    assert by_slug["cumulo"].label == "Mi contexto"


def test_cumulo_proyecta_solo_confirmed() -> None:
    _exec(
        "INSERT INTO relation_clusters (user_id, status, name, signature, blob_signature, "
        "member_count) "
        "VALUES (1, 'confirmed', 'C1', :s, :s, 2)",
        s="1" * 64,
    )
    _exec(
        "INSERT INTO relation_clusters (user_id, status, name, signature, blob_signature, "
        "member_count) "
        "VALUES (1, 'candidate', 'C2', :s, :s, 2)",
        s="2" * 64,
    )
    _exec(
        "INSERT INTO relation_clusters (user_id, status, name, signature, blob_signature, "
        "member_count) "
        "VALUES (1, 'dissolved', 'C3', :s, :s, 2)",
        s="3" * 64,
    )
    with connection() as c:
        verts = list_vertices(c, 1, slugs=("cumulo",))
    assert len(verts) == 1  # solo el confirmado proyecta
    assert verts[0].label == "C1"
    assert verts[0].kind == "cumulo"


def test_calendar_excluye_borrados() -> None:
    _exec(
        "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on, deleted) "
        "VALUES (1, 'Borrado', DATE '2026-07-02', TRUE)"
    )
    with connection() as c:
        verts = list_vertices(c, 1, slugs=("calendar",))
    assert verts == []


def test_get_vertex_y_no_vertices() -> None:
    ids = _seed_all()
    with connection() as c:
        v = get_vertex(c, 1, Ref("finance", ids["finance"]))
        assert v is not None
        assert v.label == "Rappi"
        assert v.ref == Ref("finance", ids["finance"])
        assert get_vertex(c, 1, Ref("finance", 999999)) is None  # no existe
        assert get_vertex(c, 1, Ref("inbox", 1)) is None  # inbox NO es vértice (atributo)
        assert get_vertex(c, 1, Ref("desconocido", 1)) is None  # slug desconocido


def test_scoped_por_usuario() -> None:
    _exec("INSERT INTO users (id, email, display_name) VALUES (2, 'u2@local', 'u2')")
    _exec(
        "INSERT INTO mod_finance_consolidated "
        "(user_id, direction, amount, currency, occurred_at, counterparty) "
        "VALUES (2, 'egreso', 50, 'COP', NOW(), 'Otro')"
    )
    _seed_all(1)
    with connection() as c:
        verts = list_vertices(c, 1, slugs=("finance",))
    assert len(verts) == 1
    assert verts[0].label == "Rappi"
