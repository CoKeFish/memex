"""Mantenimiento del grafo (`reconcile_graph`): reconciliación de las reales stale del
directorio/finanzas (su dato de origen cambió aunque ambos vértices vivan) + poda de huérfanas
(un extremo dejó de proyectar un vértice vivo). NO teje aristas nuevas."""

from __future__ import annotations

from memex.db import connection
from memex.relations.cooccurrence import generate_cooccurrence
from memex.relations.deterministic import (
    weave_afiliacion,
    weave_finance_consolidated,
    weave_pertenencia,
)
from memex.relations.edges import list_edges
from memex.relations.maintenance import reconcile_graph
from memex.relations.vertices import list_vertices
from tests.relations._graph_seed import (
    finance,
    hack,
    link_person_org,
    org,
    producto,
    run,
    set_parent,
)


def test_reconcile_pertenencia_quitada() -> None:
    # quitar el padre debe borrar la arista: ambos vértices siguen vivos → la poda de huérfanas no
    # la ve; la reconciliación sí (recalcula los pares vigentes HOY: el hijo ya no tiene padre).
    parent = org("Valve Corporation")
    child = producto("Celeste")
    set_parent(child, parent)
    with connection() as c:
        weave_pertenencia(c, 1, child)
        assert len(list_edges(c, 1, producer="identidades")) == 1
    set_parent(child, None)
    with connection() as c:
        stats = reconcile_graph(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.stale_pertenencia == 1
    assert edges == []


def test_reconcile_pertenencia_cambiada() -> None:
    # cambiar de padre: se teje la del padre nuevo (paso 5) y reconcile borra la del viejo.
    viejo = org("Uber")
    nuevo = org("Maddy Makes Games")
    child = producto("Celeste")
    set_parent(child, viejo)
    with connection() as c:
        weave_pertenencia(c, 1, child)
    set_parent(child, nuevo)
    with connection() as c:
        weave_pertenencia(c, 1, child)  # paso 5: teje child→nuevo
        stats = reconcile_graph(c, 1)  # mantenimiento: borra child→viejo (stale)
        edges = [
            e for e in list_edges(c, 1, producer="identidades") if e.relation_type == "pertenece_a"
        ]
    assert stats.stale_pertenencia == 1
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:org", nuevo)


def test_reconcile_afiliacion_borrada() -> None:
    p = run(
        "INSERT INTO mod_identidades (user_id, kind, display_name) "
        "VALUES (1, 'persona', 'Juan') RETURNING id"
    )
    o = org("Acme")
    link_person_org(int(p), o)
    with connection() as c:
        weave_afiliacion(c, 1, int(p))
        assert len(list_edges(c, 1, producer="identidades")) == 1
    run("DELETE FROM mod_identidades_person_orgs WHERE user_id = 1 AND person_id = :p", p=int(p))
    with connection() as c:
        stats = reconcile_graph(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.stale_afiliacion == 1
    assert edges == []


def test_reconcile_contraparte_reapuntada() -> None:
    # re-resolver la contraparte de un pago: se teje la nueva (paso 5) y reconcile borra la vieja.
    vieja = org("Uber")
    nueva = org("Uber Colombia")
    fin = finance("Uber", [12], identity_id=vieja)
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])
        assert len(list_edges(c, 1, producer="finance")) == 1
    run(
        "UPDATE mod_finance_consolidated SET counterparty_identity_id = :n WHERE id = :f",
        n=nueva,
        f=fin,
    )
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])  # paso 5: teje fin→nueva
        stats = reconcile_graph(c, 1)  # mantenimiento: borra fin→vieja (stale)
        edges = list_edges(c, 1, producer="finance")
    assert stats.stale_contraparte == 1
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:org", nueva)


def test_reconcile_idempotente_sin_cambios() -> None:
    # sin cambios en el dato, reconcile no borra nada (las reales vigentes siguen en `live`).
    p = run(
        "INSERT INTO mod_identidades (user_id, kind, display_name) "
        "VALUES (1, 'persona', 'Ana') RETURNING id"
    )
    o = org("Acme")
    link_person_org(int(p), o)
    with connection() as c:
        weave_afiliacion(c, 1, int(p))
    with connection() as c:
        stats = reconcile_graph(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.stale_afiliacion == 0
    assert stats.orphans_pruned == 0
    assert len(edges) == 1


def test_poda_huerfana_por_tombstone() -> None:
    # fin y hack co-ocurren en el correo 5 → 1 pista; tombstoneamos el consolidado: su vértice
    # desaparece (where NOT deleted) y la arista que lo tocaba queda huérfana → reconcile la barre.
    fin = finance("Rappi", [5])
    hack("HackBogota", [5])
    with connection() as c:
        generate_cooccurrence(c, 1)
        assert len(list_edges(c, 1)) == 1
    run("UPDATE mod_finance_consolidated SET deleted = TRUE WHERE id = :i", i=fin)
    with connection() as c:
        stats = reconcile_graph(c, 1)
        edges = list_edges(c, 1)
        live = {(v.slug, v.id) for v in list_vertices(c, 1)}
    assert stats.orphans_pruned == 1
    assert edges == []
    # invariante "cero huérfanas": toda arista que quede resuelve a un vértice vivo
    for e in edges:
        assert (e.src.slug, e.src.id) in live
        assert (e.dst.slug, e.dst.id) in live


def test_poda_huerfana_por_fila_borrada() -> None:
    # camino hard-delete: el hackatón se borra → su arista de co-ocurrencia queda huérfana.
    finance("Rappi", [6])
    h = hack("HackMed", [6])
    with connection() as c:
        generate_cooccurrence(c, 1)
        assert len(list_edges(c, 1)) == 1
    run("DELETE FROM mod_hackathones_events WHERE id = :i", i=h)
    with connection() as c:
        stats = reconcile_graph(c, 1)
        edges = list_edges(c, 1)
    assert stats.orphans_pruned == 1
    assert edges == []
