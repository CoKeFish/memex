"""Detección de CÚMULOS del grafo: community detection (Louvain, networkx) sobre `relation_edges`.

Un cúmulo es "solo una colección de vértices que aseguramos que están relacionados". Este módulo
SOLO los DETECTA (puro, sync, sin LLM, sin escribir): arma un grafo `networkx` ponderado con los
vértices y aristas vivos, corre Louvain, parte cada comunidad en componentes conexas (recupera
barato la garantía de Leiden: cúmulos bien conectados) y emite `CandidateCluster`s con su firma. La
persistencia y la reconciliación viven en `cluster_store` / `reconcile`; la validación, en
`clusters_llm`.

Substrato (config `MEMEX_CLUSTER_*`): por default reales=1.0, co-ocurrencia confirmada-por-LLM=0.6
y PISTAS=0.3 (sí participan: una pista es co-PRESENCIA, señal débil pero útil; el resultado se midió
insensible en [0.3,1.0] — `cluster_w_pista=0` las excluye del todo). El peso se decide por
`(status, relation_type)` porque `producer` solo no distingue (la co-ocurrencia confirmada usa el
mismo `producer='llm'`).

Determinismo: nodos insertados en orden `(slug, id)` ANTES de las aristas (el orden de nodos de
networkx = orden de inserción) + `seed` fijo en Louvain → misma partición entre corridas (con
networkx pineado a 3.6.x; un bump reordena las particiones, por eso la dependencia está fijada).
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

import networkx as nx
from sqlalchemy.engine import Connection

from memex.config import Settings, settings
from memex.logging import get_logger
from memex.relations.edges import (
    CANAL_SLUG,
    CUMULO_SLUG,
    RELTYPE_COOCURRENCIA,
    RELTYPE_MIEMBRO_DE,
    STATUS_CONFIRMED,
    STATUS_PISTA,
    Ref,
    RelationEdge,
    list_edges,
)
from memex.relations.vertices import list_vertices

_log = get_logger("memex.relations.clustering")

#: Un nodo del grafo networkx es la tupla `(slug, id)` (hashable; equivale a un `Ref`).
Node = tuple[str, int]


@dataclass(frozen=True)
class CandidateCluster:
    """Una comunidad detectada: sus vértices miembros (ordenados), su firma y si tiene alguna arista
    confirmed-real adentro (señal de anclaje para priorizar)."""

    members: tuple[Ref, ...]
    signature: str
    has_confirmed_edge: bool

    @property
    def member_set(self) -> frozenset[Ref]:
        return frozenset(self.members)


def cluster_signature(members: Iterable[Ref]) -> str:
    """sha256 del set de miembros ordenado por `(slug, id)` — la identidad estable del cúmulo
    DETECTADO. Determinista e independiente del orden de entrada. Incluye TODOS los miembros (la
    detección no conoce la poda del LLM)."""
    ordered = sorted(set(members), key=lambda r: (r.slug, r.id))
    raw = ",".join(f"{r.slug}#{r.id}" for r in ordered)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _pair_key(e: RelationEdge) -> tuple[Node, Node]:
    """Clave canónica del PAR de una arista (extremos ordenados; absorbe orientación)."""
    a: Node = (e.src.slug, e.src.id)
    b: Node = (e.dst.slug, e.dst.id)
    return (a, b) if a <= b else (b, a)


def _edge_weight(status: str, relation_type: str, cfg: Settings) -> float:
    """Peso de una arista para la clusterización, por `(status, relation_type)`. Pista → `w_pista`
    (0 por default = excluida); confirmed co-ocurrencia → `w_cooc_confirmed`; confirmed real →
    `w_confirmed`. Cualquier otro (rejected) → 0 (se descarta)."""
    if status == STATUS_PISTA:
        return cfg.cluster_w_pista
    if status == STATUS_CONFIRMED:
        if relation_type == RELTYPE_COOCURRENCIA:
            return cfg.cluster_w_cooc_confirmed
        return cfg.cluster_w_confirmed
    return 0.0


def build_cluster_graph(conn: Connection, user_id: int, cfg: Settings | None = None) -> nx.Graph:
    """Arma el grafo networkx ponderado a clusterizar: vértices vivos (excluye `cumulo`; `canal`
    SOLO si `cluster_exclude_canal`, el escape anti-hub) como nodos + aristas con peso `> 0`
    (excluye `miembro_de`, extremos excluidos, extremo no-vivo, peso ≤ 0). Multi-aristas del mismo
    par se suman con tope `pair_weight_max`. La co-ocurrencia (cualquier status) de un par que YA
    tiene arista REAL confirmada NO pesa: la conectividad la da la real y sumarla doble-contaría el
    par (las pistas redundantes se confirman en vez de borrarse — ver
    `deterministic._resolve_redundant_cooccurrence`). Quita nodos aislados (no clusterizan). Cada
    arista lleva `real` (¿es confirmed-real?) para el `has_confirmed_edge`."""
    cfg = cfg or settings
    excluded = {CUMULO_SLUG} | ({CANAL_SLUG} if cfg.cluster_exclude_canal else set())
    verts = [v for v in list_vertices(conn, user_id) if v.slug not in excluded]
    g: nx.Graph = nx.Graph()
    # Determinismo: nodos en orden (slug, id) ANTES de las aristas.
    for v in sorted(verts, key=lambda v: (v.slug, v.id)):
        g.add_node((v.slug, v.id))
    live: set[Node] = set(g.nodes)

    all_edges = [
        e
        for e in list_edges(conn, user_id)
        if e.relation_type != RELTYPE_MIEMBRO_DE
        and e.src.slug != CUMULO_SLUG
        and e.dst.slug != CUMULO_SLUG
    ]
    real_pairs: set[tuple[Node, Node]] = {
        _pair_key(e)
        for e in all_edges
        if e.status == STATUS_CONFIRMED and e.relation_type != RELTYPE_COOCURRENCIA
    }

    weights: dict[tuple[Node, Node], float] = defaultdict(float)
    real: dict[tuple[Node, Node], bool] = defaultdict(bool)
    for e in all_edges:
        w = _edge_weight(e.status, e.relation_type, cfg)
        if w <= 0:
            continue
        a: Node = (e.src.slug, e.src.id)
        b: Node = (e.dst.slug, e.dst.id)
        if a not in live or b not in live:
            continue
        key = (a, b) if a <= b else (b, a)
        if e.relation_type == RELTYPE_COOCURRENCIA and key in real_pairs:
            continue
        weights[key] += w
        real[key] = real[key] or (
            e.status == STATUS_CONFIRMED and e.relation_type != RELTYPE_COOCURRENCIA
        )
    for (a, b), w in weights.items():
        g.add_edge(a, b, weight=min(w, cfg.cluster_pair_weight_max), real=real[(a, b)])

    isolated = [n for n in list(g.nodes) if g.degree(n) == 0]
    g.remove_nodes_from(isolated)
    return g


def _louvain_split(g: nx.Graph, resolution: float, depth: int, cfg: Settings) -> list[set[Node]]:
    """Louvain + post-split por componentes conexas. Un componente `> max_members` se re-clusteriza
    a `resolution * recurse_factor` (hasta `recurse_max_depth`); el residual oversize se acepta y
    loguea (señal de tuning, NO skip silencioso)."""
    parts: list[set[Node]] = nx.community.louvain_communities(
        g, weight="weight", resolution=resolution, seed=cfg.cluster_seed
    )
    out: list[set[Node]] = []
    for part in parts:
        sub = g.subgraph(part)
        for comp_nodes in nx.connected_components(sub):
            comp = set(comp_nodes)
            if len(comp) > cfg.cluster_max_members and depth < cfg.cluster_recurse_max_depth:
                out.extend(
                    _louvain_split(
                        g.subgraph(comp), resolution * cfg.cluster_recurse_factor, depth + 1, cfg
                    )
                )
            else:
                if len(comp) > cfg.cluster_max_members:
                    _log.info(
                        "relation.cluster.oversize",
                        size=len(comp),
                        depth=depth,
                        resolution=resolution,
                    )
                out.append(comp)
    return out


def detect_clusters(g: nx.Graph, cfg: Settings | None = None) -> list[CandidateCluster]:
    """Detecta los cúmulos candidatos del grafo `g` (de `build_cluster_graph`). Filtra los menores
    a `min_size`. Devuelve la lista ordenada por clave canónica (mínimo miembro) → id local
    determinista aguas abajo."""
    cfg = cfg or settings
    if g.number_of_nodes() == 0:
        return []
    communities = _louvain_split(g, cfg.cluster_resolution, 0, cfg)
    out: list[CandidateCluster] = []
    for comp in communities:
        if len(comp) < cfg.cluster_min_size:
            continue
        members = tuple(sorted((Ref(s, i) for (s, i) in comp), key=lambda r: (r.slug, r.id)))
        has_real = any(
            g[u][v].get("real", False) for u in comp for v in g[u] if v in comp and u < v
        )
        out.append(CandidateCluster(members, cluster_signature(members), has_real))
    out.sort(key=lambda c: (c.members[0].slug, c.members[0].id))
    return out


def cluster_user(
    conn: Connection, user_id: int, cfg: Settings | None = None
) -> list[CandidateCluster]:
    """Atajo: arma el grafo del user y detecta sus cúmulos candidatos (sin persistir)."""
    cfg = cfg or settings
    g = build_cluster_graph(conn, user_id, cfg)
    clusters = detect_clusters(g, cfg)
    _log.info(
        "relation.cluster.detect",
        user_id=user_id,
        nodes=g.number_of_nodes(),
        edges=g.number_of_edges(),
        clusters=len(clusters),
    )
    return clusters
