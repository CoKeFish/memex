"""Mantenimiento del grafo (mecanismo EXTERNO, regla 8 de Módulos): el único barrido que sobrevive a
`build_relations`. NO teje aristas nuevas (eso lo hacen los módulos al escribir, paso 5) ni genera
co-ocurrencia: solo LIMPIA lo que los tejedores —aditivos— no pueden borrar.

Dos limpiezas, ambas necesarias porque los `weave_*` solo agregan:
- **Poda de huérfanas** (`prune_orphan_edges`): borra aristas con un extremo que ya no proyecta un
  vértice vivo (fila borrada / consolidado tombstoneado / identidad absorbida en un merge / cúmulo
  disuelto). Misma proyección que LEE el grafo (`list_vertices`): prune y lectura no divergen.
- **Reconciliación de stale** (`_prune_stale_reales`): borra aristas REALES cuyo DATO de origen
  cambió (contraparte re-resuelta, afiliación o padre quitado) aunque AMBOS vértices sigan vivos.
  Para saber qué sobra, recalcula los pares que el dato implica HOY (`_*_pairs`, read-only, SIN
  re-tejer) y borra las aristas que ya no están. Solo las tres reales del directorio/finanzas;
  `cumple`/`mismo_evento`/`participa_en` se limpian solo por huérfanas (igual que antes).

Disparable por job/CLI/endpoint. Idempotente.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Connection

from memex.logging import get_logger
from memex.relations.deterministic import (
    _afiliacion_pairs,
    _contraparte_pairs,
    _pertenencia_pairs,
)
from memex.relations.edges import (
    PRODUCER_FINANCE,
    PRODUCER_IDENTIDADES,
    Ref,
    list_edges,
)
from memex.relations.graph_writer import prune_edges
from memex.relations.vertices import list_vertices

_log = get_logger("memex.relations.maintenance")


@dataclass(frozen=True)
class ReconcileStats:
    """Resumen de una corrida de mantenimiento del grafo."""

    stale_afiliacion: int = 0
    stale_pertenencia: int = 0
    stale_contraparte: int = 0
    orphans_pruned: int = 0


def prune_orphan_edges(conn: Connection, user_id: int) -> int:
    """Borra de `relation_edges` toda arista con un extremo que ya no resuelve a un vértice vivo
    (consolidado tombstoneado, fila borrada, identidad absorbida en un merge…). Usa la MISMA
    proyección que LEE el grafo (`list_vertices`): prune y lectura nunca divergen. Devuelve cuántas
    borró. NOTA: un `(slug, id)` que hoy no proyecte `list_vertices` se trata como huérfano; los
    cúmulos (vértices nativos `cumulo`) YA están en `NODE_SOURCES` (solo confirmados): sus
    `miembro_de` sobreviven y las de un cúmulo disuelto (que deja de proyectar) se barren acá."""
    live = {v.ref for v in list_vertices(conn, user_id)}
    orphan_ids = [e.id for e in list_edges(conn, user_id) if e.src not in live or e.dst not in live]
    # Vía GraphWriter: borra y marca dirty el extremo que sobrevive (perdió una conexión).
    return prune_edges(conn, user_id, orphan_ids)


def _prune_stale_reales(
    conn: Connection,
    user_id: int,
    *,
    producer: str,
    relation_type: str,
    live: set[tuple[Ref, Ref]],
) -> int:
    """Reconciliación de las aristas REALES derivadas del directorio/finanzas: borra las
    (producer + relation_type) cuyo enlace de ORIGEN ya no existe (padre quitado o cambiado,
    afiliación borrada, contraparte re-resuelta). Los tejedores son aditivos y `prune_orphan_edges`
    solo ve extremos muertos: sin esto, una corrección dejaría la arista vieja viva para siempre
    (ambos vértices siguen proyectando). `live` = los pares que el dato implica HOY. Devuelve
    cuántas borró."""
    stale = [
        e.id
        for e in list_edges(conn, user_id, producer=producer)
        if e.relation_type == relation_type and (e.src, e.dst) not in live
    ]
    n = prune_edges(conn, user_id, stale)  # borra + marca dirty ambos extremos (siguen vivos)
    if n:
        _log.info(
            "relation.reconcile.pruned",
            user_id=user_id,
            producer=producer,
            relation_type=relation_type,
            pruned=n,
        )
    return n


def reconcile_graph(conn: Connection, user_id: int) -> ReconcileStats:
    """Mantenimiento del grafo (idempotente): reconcilia las tres aristas reales del directorio/
    finanzas cuyo dato de origen cambió y poda las huérfanas. NO teje aristas nuevas. La
    reconciliación corre ANTES de la poda (borra reales stale aunque ambos extremos vivan); la poda
    barre lo que quedó con un extremo muerto."""
    afil = _prune_stale_reales(
        conn,
        user_id,
        producer=PRODUCER_IDENTIDADES,
        relation_type="afiliado",
        live=set(_afiliacion_pairs(conn, user_id)),
    )
    pert = _prune_stale_reales(
        conn,
        user_id,
        producer=PRODUCER_IDENTIDADES,
        relation_type="pertenece_a",
        live=set(_pertenencia_pairs(conn, user_id)),
    )
    contra = _prune_stale_reales(
        conn,
        user_id,
        producer=PRODUCER_FINANCE,
        relation_type="contraparte",
        live=set(_contraparte_pairs(conn, user_id)),
    )
    pruned = prune_orphan_edges(conn, user_id)
    _log.info(
        "relation.reconcile.done",
        user_id=user_id,
        stale_afiliacion=afil,
        stale_pertenencia=pert,
        stale_contraparte=contra,
        orphans_pruned=pruned,
    )
    return ReconcileStats(
        stale_afiliacion=afil,
        stale_pertenencia=pert,
        stale_contraparte=contra,
        orphans_pruned=pruned,
    )
