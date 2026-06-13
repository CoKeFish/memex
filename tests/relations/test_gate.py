"""Compuerta alias-aware (`relations.gate`): reemplaza las citas. Un vértice valida si su nombre o
un alias APARECE (substring sobre texto normalizado) en el cuerpo. Identidad: nombre + alias +
identificadores; no-identidad: su `label`. Piso de longitud para no validar por ruido.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.relations.edges import Ref
from memex.relations.gate import (
    appears,
    both_endpoints_present,
    normalize_body,
    vertex_surface_forms,
)
from memex.relations.vertices import Vertex, list_vertices


def _identity(c: Any, kind: str, name: str, aliases: list[str] | None = None) -> int:
    return int(
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name, aliases) "
                "VALUES (1, :k, :n, :a) RETURNING id"
            ),
            {"k": kind, "n": name, "a": aliases or []},
        ).scalar_one()
    )


def test_identity_forms_incluye_nombre_alias_e_identificadores() -> None:
    with connection() as c:
        iid = _identity(c, "organizacion", "Google", aliases=["Big G"])
        c.execute(
            text(
                "INSERT INTO mod_identidades_identifiers "
                "(user_id, identity_id, platform, kind, value, value_norm) "
                "VALUES (1, :id, 'email', 'domain', 'google.com', 'google.com')"
            ),
            {"id": iid},
        )
        ref = Ref("identidades:org", iid)
        vmap = {v.ref: v for v in list_vertices(c, 1)}
        forms = vertex_surface_forms(c, 1, [ref], vmap)[ref]
    assert "google" in forms  # display_name normalizado
    assert "big g" in forms  # alias
    assert "google.com" in forms  # identificador (value_norm)


def test_non_identity_usa_label() -> None:
    ref = Ref("finance", 99)
    vmap = {ref: Vertex("finance", 99, "Rappi", "transaccion")}
    with connection() as c:
        forms = vertex_surface_forms(c, 1, [ref], vmap)[ref]
    assert forms == frozenset({"rappi"})


def test_min_form_len_descarta_formas_cortas() -> None:
    # una forma de 1-2 chars (ticker, inicial) no valida: ruido de substring.
    ref = Ref("finance", 1)
    vmap = {ref: Vertex("finance", 1, "Go", "transaccion")}
    with connection() as c:
        forms = vertex_surface_forms(c, 1, [ref], vmap)[ref]
    assert forms == frozenset()


def test_appears_substring_sobre_normalizado() -> None:
    body = normalize_body("Pagué en GoogleInc ayer")
    assert appears(frozenset({"google"}), body)  # 'googleinc' CONTIENE 'google' (lo que se quiere)
    assert not appears(frozenset({"amazon"}), body)


def test_appears_unaccent_y_lower() -> None:
    body = normalize_body("Reunión con José")
    assert appears(frozenset({"jose"}), body)  # unaccent + lower
    assert appears(frozenset({"reunion"}), body)


def test_both_endpoints_present() -> None:
    body = normalize_body("Juan Niebla le pagó a Acme la factura")
    assert both_endpoints_present(frozenset({"juan niebla"}), frozenset({"acme"}), body)
    # un extremo ausente → no pasa la compuerta
    assert not both_endpoints_present(frozenset({"juan niebla"}), frozenset({"zzz"}), body)
