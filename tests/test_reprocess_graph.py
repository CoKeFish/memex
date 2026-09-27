"""Etapa `graph` del reproceso/lote: ciclo DETERMINISTA del grafo tras extraer — pistas de
co-ocurrencia acotadas a los targets (incremental) + `reconcile_graph` (poda stale/huérfanas).
Sin LLM. Cubre: es la última etapa; scope por targets (un mensaje fuera del lote no genera pistas
hasta que le toque); reconcile poda una `pertenece_a` stale en la misma corrida; costo 0.
"""

from __future__ import annotations

import asyncio

from memex.db import connection
from memex.relations.deterministic import weave_pertenencia
from memex.relations.edges import list_edges
from memex.reprocess import STAGE_ORDER, reprocess
from tests.relations._graph_seed import finance, hack, org, set_parent


def test_graph_is_the_last_stage() -> None:
    # Cierra el orden de dependencia: teje sobre lo que `extract` acaba de escribir.
    assert STAGE_ORDER[-1] == "graph"


def test_graph_stage_scopes_pistas_to_targets_and_is_idempotent() -> None:
    finance("Rappi", [5])
    hack("HackBogota", [5])
    finance("Uber", [6])
    hack("HackMed", [6])

    out = asyncio.run(reprocess(1, stages=["graph"], targets=[5]))
    g = out["results"]["graph"]
    assert out["stages"] == ["graph"]
    assert g["pistas"] == 1 and g["high_fanout_skipped"] == 0
    assert g["cost_usd"] == 0.0 and out["cost_usd"] == 0.0
    with connection() as c:
        edges = list_edges(c, 1)
    # Solo el par del mensaje 5: el 6 espera a su propio lote.
    assert len(edges) == 1 and edges[0].evidence == "inbox:5"

    # Le toca al 6 → nace su par; re-correr el 5 no duplica.
    out2 = asyncio.run(reprocess(1, stages=["graph"], targets=[6, 5]))
    assert out2["results"]["graph"]["pistas"] == 2
    with connection() as c:
        assert len(list_edges(c, 1)) == 2


def test_graph_stage_reconciles_stale_pertenencia() -> None:
    child = org("Comunicaciones")
    old_parent = org("Padre viejo")
    new_parent = org("Padre nuevo")
    with connection() as c:
        set_parent(child, old_parent)
        weave_pertenencia(c, 1, child)
        set_parent(child, new_parent)
        weave_pertenencia(c, 1, child)  # aditivo: la vieja queda stale hasta el mantenimiento
        assert len(list_edges(c, 1, producer="identidades")) == 2

    out = asyncio.run(reprocess(1, stages=["graph"], targets=[]))
    assert out["results"] == {}  # sin targets no corre nada (contrato de `reprocess`)

    out = asyncio.run(reprocess(1, stages=["graph"], targets=[999]))
    g = out["results"]["graph"]
    assert g["pistas"] == 0 and g["stale_pruned"] == 1 and g["orphans_pruned"] == 0
    with connection() as c:
        edges = list_edges(c, 1, producer="identidades")
    assert [(e.src.id, e.dst.id) for e in edges] == [(child, new_parent)]
