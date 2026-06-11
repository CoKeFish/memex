from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.api.auth import current_user_id
from memex.api.schemas import (
    GraphBuildResult,
    GraphClusterResult,
    GraphClustersResponse,
    GraphClusterTimeline,
    GraphClusterValidateResult,
    GraphResponse,
)
from memex.config import settings
from memex.db import connection
from memex.logging import get_logger
from memex.relations.clusters_llm import run_cluster_partition
from memex.relations.decisions import edge_sources
from memex.relations.deterministic import build_relations, vertex_inbox_ids
from memex.relations.edges import list_edges
from memex.relations.reconcile import detect_and_reconcile
from memex.relations.timeline import cluster_timeline
from memex.relations.vertices import list_vertices
from memex.sources import kind_for_type

router = APIRouter(prefix="/graph", tags=["graph"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.graph")


def _inbox_kinds(conn: Connection, user_id: int, inbox_ids: set[int]) -> dict[int, str]:
    """Medio (email|chat|social) de cada mensaje referenciado: inbox → sources.type → SourceKind.
    Tipos sin SourceKind registrada se omiten (el front cae a «mensaje #N»)."""
    if not inbox_ids:
        return {}
    rows = conn.execute(
        text(
            "SELECT i.id AS id, s.type AS type FROM inbox i "
            "JOIN sources s ON s.id = i.source_id "
            "WHERE i.user_id = :u AND i.id = ANY(:ids)"
        ),
        {"u": user_id, "ids": sorted(inbox_ids)},
    ).mappings()
    kinds: dict[int, str] = {}
    for r in rows:
        try:
            kinds[int(r["id"])] = kind_for_type(str(r["type"])).value
        except KeyError:  # tipo sin kind (p. ej. un push custom) → el front usa el fallback
            continue
    return kinds


@router.get("", response_model=GraphResponse)
async def get_graph(
    user_id: UserID,
    status: Annotated[str | None, Query(description="pista|confirmed|rejected")] = None,
    source_inbox_id: Annotated[
        int | None,
        Query(description="enfoca: solo los vértices producidos por este mensaje + sus vecinos"),
    ] = None,
) -> dict[str, Any]:
    """El grafo del user: vértices (proyectados de las tablas `mod_*`) + aristas (`relation_edges`).

    Un vértice se direcciona por `(slug, id)`; inbox NO es vértice (es procedencia, drill-down):
    cada nodo lleva sus `source_inbox_ids` (los mensajes de los que salió) para abrir el mensaje
    original desde el grafo, y `inbox_kinds` trae el medio (email|chat|social) de cada uno. Las
    aristas llevan su `producer` y su `status` (`pista`/`confirmed`); el front filtra por `status`.
    Con `source_inbox_id` el grafo se ENFOCA en lo que produjo ese mensaje (sentido inverso del
    drill-down nodo→mensaje): sus vértices + los vecinos a un salto. Solo LECTURA: no dispara el
    armado (POST /graph/build).
    """
    with connection() as conn:
        verts = list_vertices(conn, user_id)
        edges = list_edges(conn, user_id, status=status)
        prov = vertex_inbox_ids(conn, user_id)
        if source_inbox_id is not None:
            # Vértices producidos por ESE mensaje (semilla) + las aristas que los tocan; se
            # conservan los vecinos al otro extremo para no dejar aristas colgando. Mensaje sin
            # nada → grafo vacío.
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
        # Procedencia por ARISTA (relation_edge_sources): todos los mensajes que generaron cada
        # pista de co-ocurrencia — el drill-down de aristas, espejo del de nodos.
        edge_srcs = edge_sources(conn, [e.id for e in edges])
        # El medio de cada mensaje referenciado por el set FINAL de vértices Y aristas (por eso se
        # calcula acá adentro, después del foco/poda).
        referenced: set[int] = set()
        for v in verts:
            referenced |= prov.get(v.ref, set())
        for ids in edge_srcs.values():
            referenced |= ids
        inbox_kinds = _inbox_kinds(conn, user_id, referenced)
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
            "source_inbox_ids": sorted(edge_srcs.get(e.id, set())),
        }
        for e in edges
    ]
    _log.info(
        "graph.read",
        user_id=user_id,
        nodes=len(nodes),
        edges=len(out_edges),
        inbox_kinds=len(inbox_kinds),
    )
    return {"nodes": nodes, "edges": out_edges, "inbox_kinds": inbox_kinds}


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
        participa=stats.participa_reales,
        canales=stats.canales,
        chat_senders=stats.chat_senders,
        skipped=stats.high_fanout_skipped,
        orphans_pruned=stats.orphans_pruned,
        stale_pruned=stats.stale_pruned,
    )
    return {
        "cooccurrence_pistas": stats.cooccurrence_pistas,
        "afiliacion_reales": stats.afiliacion_reales,
        "pertenencia_reales": stats.pertenencia_reales,
        "contraparte_reales": stats.contraparte_reales,
        "cumple_reales": stats.cumple_reales,
        "participa_reales": stats.participa_reales,
        "high_fanout_skipped": stats.high_fanout_skipped,
        "orphans_pruned": stats.orphans_pruned,
        "stale_pruned": stats.stale_pruned,
        "cluster_edges": stats.cluster_edges,
        "chat_senders": stats.chat_senders,
        "canales": stats.canales,
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


@router.get("/clusters/{cluster_id}/timeline", response_model=GraphClusterTimeline)
async def cluster_timeline_view(cluster_id: int, user_id: UserID) -> dict[str, Any]:
    """Cronología (story) de un cúmulo CONFIRMADO: sus miembros fechados como sucesos ordenados +
    el elenco (miembros sin fecha: identidades, hábitos). Solo lectura. 404 si no existe / no
    confirmed. Fechas en hora local (America/Bogota) con `precision` para mostrar hora solo si es
    real."""
    with connection() as conn:
        tl = cluster_timeline(conn, user_id, cluster_id)
        inbox_kinds: dict[int, str] = {}
        if tl is not None:
            referenced = {i for e in tl.events for i in e.source_inbox_ids}
            referenced |= {i for a in tl.actors for i in a.source_inbox_ids}
            inbox_kinds = _inbox_kinds(conn, user_id, referenced)
    if tl is None:
        raise HTTPException(status_code=404, detail="cúmulo no encontrado o no confirmado")
    _log.info(
        "graph.cluster.timeline.api",
        user_id=user_id,
        cluster_id=cluster_id,
        events=len(tl.events),
        actors=len(tl.actors),
    )
    return {
        "cluster": {
            "id": tl.cluster.id,
            "name": tl.cluster.name,
            "description": tl.cluster.description,
            "confidence": tl.cluster.confidence,
            "member_count": tl.cluster.member_count,
        },
        "events": [
            {
                "slug": e.slug,
                "id": e.id,
                "kind": e.kind,
                "label": e.label,
                "at": e.at,
                "precision": e.precision,
                "source_inbox_ids": e.source_inbox_ids,
            }
            for e in tl.events
        ],
        "actors": [
            {
                "slug": a.slug,
                "id": a.id,
                "kind": a.kind,
                "label": a.label,
                "source_inbox_ids": a.source_inbox_ids,
            }
            for a in tl.actors
        ],
        "inbox_kinds": inbox_kinds,
    }
