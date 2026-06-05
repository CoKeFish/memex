from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from memex.api.auth import current_user_id
from memex.api.schemas import GraphBuildResult, GraphResponse
from memex.config import settings
from memex.db import connection
from memex.logging import get_logger
from memex.relations.deterministic import build_relations, vertex_inbox_ids
from memex.relations.edges import list_edges
from memex.relations.vertices import list_vertices

router = APIRouter(prefix="/graph", tags=["graph"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.graph")


@router.get("", response_model=GraphResponse)
async def get_graph(
    user_id: UserID,
    status: Annotated[str | None, Query(description="pista|confirmed|rejected")] = None,
) -> dict[str, Any]:
    """El grafo del user: vértices (proyectados de las tablas `mod_*`) + aristas (`relation_edges`).

    Un vértice se direcciona por `(slug, id)`; inbox NO es vértice (es procedencia, drill-down):
    cada nodo lleva sus `source_inbox_ids` (los mensajes de los que salió) para abrir el correo
    original desde el grafo. Las aristas llevan su `producer` y su `status` (`pista`/`confirmed`);
    el front filtra por `status`. Solo LECTURA: no dispara el armado (POST /graph/build).
    """
    with connection() as conn:
        verts = list_vertices(conn, user_id)
        edges = list_edges(conn, user_id, status=status)
        prov = vertex_inbox_ids(conn, user_id)
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
        skipped=stats.high_fanout_skipped,
    )
    return {
        "cooccurrence_pistas": stats.cooccurrence_pistas,
        "afiliacion_reales": stats.afiliacion_reales,
        "pertenencia_reales": stats.pertenencia_reales,
        "contraparte_reales": stats.contraparte_reales,
        "high_fanout_skipped": stats.high_fanout_skipped,
    }
