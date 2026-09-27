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
    GROUNDED_BY_PROVENANCE,
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


def test_non_identity_valida_por_procedencia_no_por_label() -> None:
    """Un evento extraído del mensaje ya está en él por construcción; su título es paráfrasis del
    extractor («Curso Soldadura SMT»), no cita: exigirlo literal bloqueaba toda relación con
    eventos (42/42 gated en la corrida del 2026-09-05)."""
    ref = Ref("calendar", 99)
    vmap = {ref: Vertex("calendar", 99, "Curso Soldadura SMT", "evento")}
    with connection() as c:
        forms = vertex_surface_forms(c, 1, [ref], vmap)[ref]
    assert forms == frozenset({GROUNDED_BY_PROVENANCE})
    assert appears(forms, "cuerpo que no menciona el titulo")
    # Y el par pasa la compuerta solo si la IDENTIDAD del otro extremo sí aparece.
    assert both_endpoints_present(forms, frozenset({"electronica"}), "carrera de electronica")
    assert not both_endpoints_present(forms, frozenset({"electronica"}), "otro texto")


def test_min_form_len_descarta_formas_cortas() -> None:
    # una forma de 1-2 chars (ticker, inicial) no valida: ruido de substring.
    with connection() as c:
        iid = _identity(c, "organizacion", "Go")
        ref = Ref("identidades:org", iid)
        vmap = {v.ref: v for v in list_vertices(c, 1)}
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
