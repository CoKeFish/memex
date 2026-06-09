from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    GraphBuildResult,
    GraphClusterResult,
    GraphClustersResponse,
    GraphClusterValidateResult,
    GraphResponse,
)
from memex.config import settings
from memex.db import connection
from memex.logging import get_logger
from memex.relations.clusters_llm import run_cluster_partition
from memex.relations.deterministic import build_relations, vertex_inbox_ids
from memex.relations.edges import list_edges
from memex.relations.reconcile import detect_and_reconcile
from memex.relations.vertices import list_vertices

router = APIRouter(prefix="/graph", tags=["graph"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.graph")


@router.get("", response_model=GraphResponse)
async def get_graph(
    user_id: UserID,
    status: Annotated[str | None, Query(description="pista|confirmed|rejected")] = None,
    source_inbox_id: Annotated[
        int | None,
        Query(description="enfoca: solo los vértices producidos por este correo + sus vecinos"),
    ] = None,
) -> dict[str, Any]:
    """El grafo del user: vértices (proyectados de las tablas `mod_*`) + aristas (`relation_edges`).

    Un vértice se direcciona por `(slug, id)`; inbox NO es vértice (es procedencia, drill-down):
    cada nodo lleva sus `source_inbox_ids` (los mensajes de los que salió) para abrir el correo
    original desde el grafo. Las aristas llevan su `producer` y su `status` (`pista`/`confirmed`);
    el front filtra por `status`. Con `source_inbox_id` el grafo se ENFOCA en lo que produjo ese
    correo (sentido inverso del drill-down nodo→correo): sus vértices + los vecinos a un salto. Solo
    LECTURA: no dispara el armado (POST /graph/build).
    """
    with connection() as conn:
        verts = list_vertices(conn, user_id)
        edges = list_edges(conn, user_id, status=status)
        prov = vertex_inbox_ids(conn, user_id)
    if source_inbox_id is not None:
        # Vértices producidos por ESE correo (semilla) + las aristas que los tocan; se conservan los
        # vecinos al otro extremo para no dejar aristas colgando. Correo sin nada → grafo vacío.
        seed = {ref for ref, ids in prov.items() if source_inbox_id in ids}
        edges = [e for e in edges if e.src in seed or e.dst in seed]
        keep = set(seed)
        for e in edges:
            keep.add(e.src)
            keep.add(e.dst)
        verts = [v for v in verts if v.ref in keep]
    # Poda de aristas huérfanas: ambos extremos deben ser vértices PRESENTES (vivos en default,
    # dentro del subgrafo en foco). Cruza-filtra contra el set final de `verts` → nunca se sirve
    # una arista a un nodo ausente (consolidado tombstoneado / fila borrada / merge sin GC aún).
    present = {v.ref for v in verts}
    edges = [e for e in edges if e.src in present and e.dst in present]
    nodes = [
        {
            "slug": v.slug,
            "id": v.id,
            "label": v.label,
            "kind": v.kind,
            "source_inbox_ids": sorted(prov.get(v.ref, set())),
        }
        for v in verts
    ]
    out_edges = [
        {
            "id": e.id,
            "src_slug": e.src.slug,
            "src_id": e.src.id,
            "dst_slug": e.dst.slug,
            "dst_id": e.dst.id,
            "relation_type": e.relation_type,
            "producer": e.producer,
            "status": e.status,
            "confidence": float(e.confidence) if e.confidence is not None else None,
            "evidence": e.evidence,
        }
        for e in edges
    ]
    _log.info("graph.read", user_id=user_id, nodes=len(nodes), edges=len(out_edges))
    return {"nodes": nodes, "edges": out_edges}


@router.post("/build", response_model=GraphBuildResult)
async def build_graph(user_id: UserID) -> dict[str, Any]:
    """Corre el paso de relaciones DETERMINISTAS (on-demand, explícito): materializa pistas de
    co-ocurrencia + afiliaciones reales sobre lo ya guardado. Idempotente y sin LLM. NO dispara
    extracción/consolidación (consume lo disponible). El tope de fan-out es configurable
    (`MEMEX_COOCCURRENCE_CAP`)."""
    with connection() as conn:
        stats = build_relations(conn, user_id, cooccurrence_cap=settings.cooccurrence_cap)
    _log.info(
        "graph.build.api",
        user_id=user_id,
        pistas=stats.cooccurrence_pistas,
        reales=stats.afiliacion_reales,
        pertenencia=stats.pertenencia_reales,
        contraparte=stats.contraparte_reales,
        cumple=stats.cumple_reales,
        skipped=stats.high_fanout_skipped,
        orphans_pruned=stats.orphans_pruned,
    )
    return {
        "cooccurrence_pistas": stats.cooccurrence_pistas,
        "afiliacion_reales": stats.afiliacion_reales,
        "pertenencia_reales": stats.pertenencia_reales,
        "contraparte_reales": stats.contraparte_reales,
        "cumple_reales": stats.cumple_reales,
        "high_fanout_skipped": stats.high_fanout_skipped,
        "orphans_pruned": stats.orphans_pruned,
        "cluster_edges": stats.cluster_edges,
    }


@router.post("/cluster", response_model=GraphClusterResult)
async def cluster_graph(user_id: UserID) -> dict[str, Any]:
    """Detecta los cúmulos (Louvain) y los reconcilia contra lo persistido. On-demand, SIN LLM e
    idempotente: re-detectar la misma partición no cambia nada. NO dispara el armado del grafo
    (POST /graph/build) ni la validación LLM (POST /graph/cluster/validate)."""
    with connection() as conn:
        stats = detect_and_reconcile(conn, user_id)
    _log.info(
        "graph.cluster.api",
        user_id=user_id,
        detected=stats.detected,
        new_candidates=stats.new_candidates,
        dissolved=stats.dissolved,
    )
    return {
        "detected": stats.detected,
        "matched_same": stats.matched_same,
        "matched_drift": stats.matched_drift,
        "new_candidates": stats.new_candidates,
        "memo_skipped": stats.memo_skipped,
        "deleted": stats.deleted,
        "dissolved": stats.dissolved,
    }


@router.post("/cluster/validate", response_model=GraphClusterValidateResult)
async def validate_clusters(
    user_id: UserID,
    limit: Annotated[int | None, Query(description="máximo de blobs a particionar")] = None,
) -> dict[str, Any]:
    """Parte con el LLM los blobs `candidate`: cada blob → N contextos (hijos confirmed),
    preservando la identidad de los hijos al re-particionar, promoviendo las pistas intra-grupo y
    materializando las aristas `miembro_de`. Usa el LLM (cuesta); on-demand. Solo los pendientes."""
    stats = await run_cluster_partition(user_id, limit=limit)
    _log.info(
        "graph.cluster.partition.api",
        user_id=user_id,
        blobs=stats.blobs,
        groups=stats.groups,
        errors=stats.errors,
    )
    return {
        "blobs": stats.blobs,
        "groups": stats.groups,
        "created": stats.created,
        "synced": stats.synced,
        "dissolved": stats.dissolved,
        "rejected": stats.rejected,
        "promoted": stats.promoted,
        "skipped": stats.skipped,
        "errors": stats.errors,
        "llm_calls": stats.cost.calls,
        "cost_usd": float(stats.cost.cost_usd),
    }


@router.get("/clusters", response_model=GraphClustersResponse)
async def list_clusters(
    user_id: UserID,
    status: Annotated[str | None, Query(description="filtra por estado del cúmulo")] = None,
) -> dict[str, Any]:
    """Lista los cúmulos del user (opcionalmente filtrados por `status`)."""
    sql = (
        "SELECT id, status, name, description, confidence, member_count "
        "FROM relation_clusters WHERE user_id = :u"
    )
    params: dict[str, Any] = {"u": user_id}
    if status is not None:
        sql += " AND status = :st"
        params["st"] = status
    sql += " ORDER BY status, id"
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    clusters = [
        {
            "id": int(r["id"]),
            "status": str(r["status"]),
            "name": str(r["name"]),
            "description": str(r["description"]),
            "confidence": float(r["confidence"]) if r["confidence"] is not None else None,
            "member_count": int(r["member_count"]),
        }
        for r in rows
    ]
    _log.info("graph.clusters.api", user_id=user_id, clusters=len(clusters))
    return {"clusters": clusters}
