"""Tejido de las aristas REALES (paso 5): `weave_afiliacion`, `weave_pertenencia`,
`weave_finance_consolidated` (contraparte). Cada módulo las teje al escribir; acá se seedea el dato
y se llama la weave pública directo (idempotente, dirigida, con el slug correcto del extremo)."""

from __future__ import annotations

from memex.db import connection
from memex.relations.deterministic import (
    weave_afiliacion,
    weave_finance_consolidated,
    weave_pertenencia,
)
from memex.relations.edges import list_edges
from tests.relations._graph_seed import finance, link_person_org, org, person, producto, set_parent


def test_afiliacion_real_persona_org() -> None:
    p = person("Juan")
    o = org("Acme")
    link_person_org(p, o)
    with connection() as c:
        n = weave_afiliacion(c, 1, p)
        edges = list_edges(c, 1, producer="identidades")
    assert n == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.verdict == "confirmed"
    assert e.relation_type == "afiliado"
    assert (e.src.slug, e.src.id) == ("identidades:person", p)
    assert (e.dst.slug, e.dst.id) == ("identidades:org", o)


def test_pertenencia_real_sub_padre() -> None:
    parent = org("Valve Corporation")
    child = org("Steam")
    set_parent(child, parent)
    with connection() as c:
        n = weave_pertenencia(c, 1, child)
        edges = list_edges(c, 1, producer="identidades")
    assert n == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.verdict == "confirmed"
    assert e.relation_type == "pertenece_a"
    assert (e.src.slug, e.src.id) == ("identidades:org", child)  # dirigida: hijo → padre
    assert (e.dst.slug, e.dst.id) == ("identidades:org", parent)


def test_pertenencia_producto_a_empresa() -> None:
    # producto→empresa con kinds reales: la arista usa el slug identidades:producto del hijo.
    parent = org("Valve Corporation")
    child = producto("Steam")
    set_parent(child, parent)
    with connection() as c:
        weave_pertenencia(c, 1, child)
        edges = list_edges(c, 1, producer="identidades")
    assert len(edges) == 1
    e = edges[0]
    assert e.relation_type == "pertenece_a"
    assert (e.src.slug, e.src.id) == ("identidades:producto", child)
    assert (e.dst.slug, e.dst.id) == ("identidades:org", parent)


def test_pertenencia_sin_padre_no_edge() -> None:
    # un hijo sin `parent_identity_id` no teje nada (la weave es no-op).
    child = producto("Celeste")
    with connection() as c:
        n = weave_pertenencia(c, 1, child)
        edges = list_edges(c, 1, producer="identidades")
    assert n == 0
    assert edges == []


def test_contraparte_real_cobro_a_identidad() -> None:
    # un cobro CONSOLIDADO cuya contraparte resolvió a una identidad → arista confirmed
    # cobro→identidad (el enlace por identidad entre finanzas y el directorio).
    o = org("Uber")
    fin = finance("Uber", [12], identity_id=o)
    with connection() as c:
        contraparte, same_event = weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert contraparte == 1
    assert same_event == 0
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "finance"
    assert e.verdict == "confirmed"
    assert e.relation_type == "contraparte"
    assert (e.src.slug, e.src.id) == ("finance", fin)  # dirigida: cobro → quién cobró/pagó
    assert (e.dst.slug, e.dst.id) == ("identidades:org", o)


def test_contraparte_sin_identidad_no_edge() -> None:
    # cobro sin counterparty_identity_id (no resolvió) → no hay arista de contraparte.
    fin = finance("Comercio X", [13])
    with connection() as c:
        contraparte, _ = weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert contraparte == 0
    assert edges == []


def test_contraparte_persona() -> None:
    # contraparte persona (ej. una transferencia a alguien) → arista a identidades:person.
    p = person("Juan Perez")
    fin = finance("Juan Perez", [14], identity_id=p)
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:person", p)
    assert (edges[0].src.slug, edges[0].src.id) == ("finance", fin)


def test_contraparte_a_producto_via_id() -> None:
    # un counterparty_identity_id que apunta a un producto → la arista usa el slug
    # identidades:producto y no queda huérfana.
    prod = producto("Steam")
    fin = finance("Steam", [22], identity_id=prod)
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert len(edges) == 1
    assert (edges[0].src.slug, edges[0].src.id) == ("finance", fin)
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:producto", prod)


def test_weave_idempotente() -> None:
    # re-tejer no duplica (ON CONFLICT lógico de propose_edge).
    p = person("Ana")
    o = org("Acme")
    link_person_org(p, o)
    with connection() as c:
        weave_afiliacion(c, 1, p)
        weave_afiliacion(c, 1, p)
        edges = list_edges(c, 1, producer="identidades")
    assert len(edges) == 1
