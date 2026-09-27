"""Tejido de asuntos: asunto→elenco («involucra», con procedencia) y asunto→asunto tipadas;
reconciliación poda lo que dejó de estar vigente; el asunto queda fuera del grafo a clusterizar."""

from __future__ import annotations

from typing import Any

from memex.db import connection
from memex.modules.asuntos import store
from memex.relations.clustering import build_cluster_graph
from memex.relations.decisions import edge_sources
from memex.relations.deterministic import weave_asuntos
from memex.relations.edges import PRODUCER_ASUNTOS, list_edges
from memex.relations.maintenance import reconcile_graph
from tests.asuntos._seed import asunto, evidence, msg, source
from tests.relations._graph_seed import mention, person


def test_weave_involucra_y_relaciones_con_procedencia(conn: Any) -> None:
    src = source()
    a = asunto("Impact Studio")
    g = asunto("Grado", kind="tramite")
    cand = asunto("cand", status="candidato")
    m1, m2, m3 = msg(src, subject="1"), msg(src, subject="2"), msg(src, subject="3")
    evidence(a, m1)
    evidence(a, m2)
    evidence(cand, m3)
    p = person("Ana")
    mention(p, [m1, m2])
    q = person("Otro")
    mention(q, [m3])  # solo en el candidato → sin arista
    with connection() as c:
        store.add_relation(c, 1, a, g, "continua")
        n = weave_asuntos(c, 1, [a, g])
        edges = list_edges(c, 1, producer=PRODUCER_ASUNTOS)
        srcs = edge_sources(c, [e.id for e in edges])
        assert weave_asuntos(c, 1) == 0  # idempotente
    assert n == 2
    by = {(e.src.slug, e.src.id, e.dst.slug, e.dst.id): e for e in edges}
    inv = by[("asunto", a, "identidades:person", p)]
    assert inv.relation_type == "involucra" and inv.verdict == "confirmed"
    assert srcs[inv.id] == {m1, m2}
    rel = by[("asunto", a, "asunto", g)]
    assert rel.relation_type == "continua"
    assert ("asunto", cand) not in {(k[0], k[1]) for k in by}


def test_reconcile_poda_involucra_stale_y_cluster_excluye_asunto(conn: Any) -> None:
    src = source()
    a = asunto("X")
    m1 = msg(src, subject="1")
    evidence(a, m1)
    p = person("Ana")
    mention(p, [m1])
    with connection() as c:
        assert weave_asuntos(c, 1, [a]) == 1
        [ev] = store.evidence_of(c, 1, a)
        store.reject_evidence(c, 1, ev.id)
        st = reconcile_graph(c, 1)
        assert st.stale_asuntos == 1
        assert list_edges(c, 1, producer=PRODUCER_ASUNTOS) == []
        g = build_cluster_graph(c, 1)
    assert all(node[0] != "asunto" for node in g.nodes)
