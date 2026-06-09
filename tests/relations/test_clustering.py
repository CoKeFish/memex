"""Detección de cúmulos (Louvain): comunidades, post-split por componentes, min_size, exclusiones,
peso por (status, relation_type) y DETERMINISMO. Sin LLM; grafos chicos sobre vértices reales."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.clustering import build_cluster_graph, cluster_signature, detect_clusters
from memex.relations.edges import (
    PRODUCER_IDENTIDADES,
    PRODUCER_INBOX,
    PRODUCER_LLM,
    RELTYPE_COOCURRENCIA,
    RELTYPE_MIEMBRO_DE,
    STATUS_CONFIRMED,
    STATUS_PISTA,
    Ref,
    propose_edge,
)


def _person(conn: Connection, name: str) -> Ref:
    pid = conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'persona', :n) RETURNING id"
        ),
        {"n": name},
    ).scalar_one()
    return Ref("identidades:person", int(pid))


def _edge(
    conn: Connection,
    a: Ref,
    b: Ref,
    *,
    status: str = STATUS_CONFIRMED,
    rt: str = "afiliado",
    producer: str = PRODUCER_IDENTIDADES,
) -> None:
    propose_edge(conn, 1, a, b, producer=producer, relation_type=rt, status=status)


def _sigs(conn: Connection) -> list[str]:
    g = build_cluster_graph(conn, 1)
    return [c.signature for c in detect_clusters(g)]


def test_dos_triangulos_puenteados_dos_comunidades(conn: Connection) -> None:
    p = [_person(conn, f"P{i}") for i in range(6)]
    # triángulo A
    _edge(conn, p[0], p[1])
    _edge(conn, p[1], p[2])
    _edge(conn, p[0], p[2])
    # triángulo B
    _edge(conn, p[3], p[4])
    _edge(conn, p[4], p[5])
    _edge(conn, p[3], p[5])
    # puente único entre ambos
    _edge(conn, p[2], p[3])

    g = build_cluster_graph(conn, 1)
    clusters = detect_clusters(g)
    assert len(clusters) == 2
    sets = sorted((frozenset(c.members) for c in clusters), key=lambda s: min(r.id for r in s))
    assert sets[0] == frozenset({p[0], p[1], p[2]})
    assert sets[1] == frozenset({p[3], p[4], p[5]})


def test_min_size_descarta_pares(conn: Connection) -> None:
    a, b = _person(conn, "A"), _person(conn, "B")
    _edge(conn, a, b)  # comunidad de 2 < min_size(3)
    g = build_cluster_graph(conn, 1)
    assert detect_clusters(g) == []


def test_pistas_excluidas_por_default(conn: Connection) -> None:
    # w_pista=0 → un triángulo de PISTAS de co-ocurrencia no produce aristas ni cúmulos.
    p = [_person(conn, f"P{i}") for i in range(3)]
    for i in range(3):
        _edge(
            conn,
            p[i],
            p[(i + 1) % 3],
            status=STATUS_PISTA,
            rt=RELTYPE_COOCURRENCIA,
            producer=PRODUCER_INBOX,
        )
    g = build_cluster_graph(conn, 1)
    assert g.number_of_edges() == 0
    assert detect_clusters(g) == []


def test_cooccurrencia_confirmada_si_participa(conn: Connection) -> None:
    # confirmed co-ocurrencia tiene peso 0.6 (> 0) → un triángulo sí forma cúmulo.
    p = [_person(conn, f"P{i}") for i in range(3)]
    for i in range(3):
        _edge(
            conn,
            p[i],
            p[(i + 1) % 3],
            status=STATUS_CONFIRMED,
            rt=RELTYPE_COOCURRENCIA,
            producer=PRODUCER_LLM,
        )
    g = build_cluster_graph(conn, 1)
    clusters = detect_clusters(g)
    assert len(clusters) == 1
    assert frozenset(clusters[0].members) == frozenset(p)


def test_excluye_miembro_de(conn: Connection) -> None:
    # una arista miembro_de NO entra al grafo de clusterización → sus extremos quedan aislados.
    a, b, c = _person(conn, "A"), _person(conn, "B"), _person(conn, "C")
    _edge(conn, a, b, rt=RELTYPE_MIEMBRO_DE, producer=PRODUCER_LLM)
    _edge(conn, b, c, rt=RELTYPE_MIEMBRO_DE, producer=PRODUCER_LLM)
    g = build_cluster_graph(conn, 1)
    assert g.number_of_edges() == 0


def test_has_confirmed_edge(conn: Connection) -> None:
    # un triángulo con al menos una arista confirmed-real → has_confirmed_edge True.
    p = [_person(conn, f"P{i}") for i in range(3)]
    _edge(conn, p[0], p[1], status=STATUS_CONFIRMED, rt="afiliado")
    _edge(conn, p[1], p[2], status=STATUS_CONFIRMED, rt="afiliado")
    _edge(conn, p[0], p[2], status=STATUS_CONFIRMED, rt="afiliado")
    g = build_cluster_graph(conn, 1)
    clusters = detect_clusters(g)
    assert len(clusters) == 1
    assert clusters[0].has_confirmed_edge is True


def test_determinismo_dos_corridas(conn: Connection) -> None:
    p = [_person(conn, f"P{i}") for i in range(6)]
    _edge(conn, p[0], p[1])
    _edge(conn, p[1], p[2])
    _edge(conn, p[0], p[2])
    _edge(conn, p[3], p[4])
    _edge(conn, p[4], p[5])
    _edge(conn, p[3], p[5])
    _edge(conn, p[2], p[3])
    assert _sigs(conn) == _sigs(conn)  # firmas E orden idénticos entre corridas


def test_grafo_vacio(conn: Connection) -> None:
    g = build_cluster_graph(conn, 1)
    assert g.number_of_nodes() == 0
    assert detect_clusters(g) == []


def test_signature_independiente_del_orden() -> None:
    a = cluster_signature([Ref("finance", 2), Ref("identidades:person", 1)])
    b = cluster_signature([Ref("identidades:person", 1), Ref("finance", 2)])
    assert a == b
