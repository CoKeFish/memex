"""GraphWriter — el ÚNICO punto de paso para mutar el grafo (aristas / vértices / cúmulos).

Por qué existe (ADR-021, COBERTURA del marcado incremental): el groundwork `dirty`
(`relation_vertex_state` + `relation_edges.dirty`) solo sirve si TODA mutación deja el delta
COMPLETO para que un futuro mantenedor incremental sepa qué reprocesar. Marcar disperso deja huecos
—fusión de identidad, borrado y rechazo no avisan a los vecinos afectados, cuyo estado cambió sin
que ellos mismos cambiaran—. Este módulo centraliza la disciplina: cada mutación

  1. captura el alcance afectado ANTES de mutar (para las destructivas: después ya no es legible),
  2. escribe (delegando en las primitivas de `memex.relations.edges`),
  3. marca `dirty` el/los elementos, y
  4. PROPAGA el dirty a los vecinos hasta `graph_propagate_dirty_hops` saltos (config, default 1).

ALCANCE: este módulo SOLO deja la información de "qué cambió" lista y confiable. NO consume el
dirty (no re-clusteriza, no corre lint, no re-confirma) — ese mantenedor incremental es trabajo
aparte y deliberadamente fuera de alcance.

DISCIPLINA: nadie fuera de este módulo debería escribir crudo a las tablas del grafo ni mutar un
vértice del grafo sin pasar por acá. Las primitivas de `edges` quedarán privadas detrás de esta
capa en el slice de enforcement; el lint repo-local lo garantizará.

Nota de construcción: este slice cubre ARISTAS y el marcado de VÉRTICES (add/update).
`merge_vertices`/`delete_vertex` y el ciclo de CÚMULOS (`add/sync/dissolve/reject_cluster`) se
agregan en sus slices de ruteo, donde se diseñan contra sus llamadores reales.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import settings
from memex.logging import get_logger
from memex.relations.edges import (
    PROVENANCE_EXTRACTED,
    VERDICT_AMBIGUOUS,
    VERDICT_REJECTED,
    Ref,
    edges_touching,
    mark_vertices_dirty,
    propose_edge,
    resolve_edge,
)

_log = get_logger("memex.relations.graph_writer")


# --- Propagación del dirty a vecinos (BFS por aristas, hasta N saltos) ------------------- #
def _dirty_closure(conn: Connection, user_id: int, seeds: set[Ref], hops: int) -> set[Ref]:
    """Los `seeds` MÁS sus vecinos hasta `hops` saltos (BFS sobre `edges_touching`). `hops <= 0`
    devuelve solo los seeds. Recorre CUALQUIER arista, incluida `miembro_de` (a 1 salto alcanza el
    vértice cúmulo y a 2 los co-miembros): la membresía de cúmulo se propaga sin caso aparte.
    """
    result = set(seeds)
    frontier = set(seeds)
    for _ in range(max(0, hops)):
        nxt: set[Ref] = set()
        for ref in frontier:
            for e in edges_touching(conn, user_id, ref):
                other = e.dst if e.src == ref else e.src
                if other not in result:
                    nxt.add(other)
        if not nxt:
            break
        result |= nxt
        frontier = nxt
    return result


def mark_dirty(
    conn: Connection, user_id: int, seeds: Iterable[Ref], *, hops: int | None = None
) -> None:
    """Marca `dirty` los `seeds` y propaga a sus vecinos hasta `hops` saltos (default: config
    `graph_propagate_dirty_hops`). Es la pieza que TODA mutación llama para dejar el delta completo.
    Sobre-marcar es seguro: el mantenedor reprocesa de más, nunca de menos."""
    seed_set = set(seeds)
    if not seed_set:
        return
    h = settings.graph_propagate_dirty_hops if hops is None else hops
    mark_vertices_dirty(conn, user_id, list(_dirty_closure(conn, user_id, seed_set, h)))


def _edge_endpoints(conn: Connection, user_id: int, edge_id: int) -> tuple[Ref, Ref] | None:
    """Los dos extremos `(src, dst)` de una arista por id, o `None` si no existe. Se lee ANTES de
    borrar/transicionar para tener a quién propagar el dirty."""
    row = (
        conn.execute(
            text(
                "SELECT src_slug, src_id, dst_slug, dst_id FROM relation_edges "
                "WHERE id = :id AND user_id = :uid"
            ),
            {"id": edge_id, "uid": user_id},
        )
        .mappings()
        .first()
    )
    if row is None:
        return None
    return (
        Ref(str(row["src_slug"]), int(row["src_id"])),
        Ref(str(row["dst_slug"]), int(row["dst_id"])),
    )


# --- ARISTAS ----------------------------------------------------------------------------- #
def add_edge(
    conn: Connection,
    user_id: int,
    src: Ref,
    dst: Ref,
    *,
    producer: str,
    relation_type: str = "",
    provenance: str = PROVENANCE_EXTRACTED,
    verdict: str = VERDICT_AMBIGUOUS,
    relation: str = "",
    confidence: Decimal | None = None,
    evidence: str = "",
    seed_tag: str | None = None,
) -> int:
    """Crea (idempotente) una arista y marca dirty sus dos extremos (+ vecinos). Reemplaza los
    `propose_edge` sueltos. Misma firma que `propose_edge`, devuelve el id."""
    edge_id = propose_edge(
        conn,
        user_id,
        src,
        dst,
        producer=producer,
        relation_type=relation_type,
        provenance=provenance,
        verdict=verdict,
        relation=relation,
        confidence=confidence,
        evidence=evidence,
        seed_tag=seed_tag,
    )
    mark_dirty(conn, user_id, [src, dst])
    return edge_id


def update_verdict(
    conn: Connection,
    user_id: int,
    edge_id: int,
    *,
    verdict: str,
    provenance: str,
    relation: str | None = None,
    confidence: Decimal | None = None,
    evidence: str | None = None,
) -> bool:
    """Transiciona el veredicto (confirm/reject) y marca dirty AMBOS extremos — para confirm Y para
    reject por igual (un rechazo le quita peso al clustering del vecindario: ese era el hueco).
    Reemplaza los `resolve_edge` sueltos. Devuelve si cambió algo."""
    ends = _edge_endpoints(conn, user_id, edge_id)
    if ends is None:  # no existe (o es de otro user) — simetría con delete_edge
        return False
    changed = resolve_edge(
        conn,
        edge_id,
        verdict=verdict,
        provenance=provenance,
        relation=relation,
        confidence=confidence,
        evidence=evidence,
    )
    if changed:
        mark_dirty(conn, user_id, [ends[0], ends[1]])
    return changed


def delete_edge(conn: Connection, user_id: int, edge_id: int) -> bool:
    """Borra una arista por id, capturando sus extremos ANTES del DELETE y marcándolos dirty
    (perdieron una conexión → su clustering cambió). Devuelve si borró."""
    ends = _edge_endpoints(conn, user_id, edge_id)
    if ends is None:
        return False
    conn.execute(
        text("DELETE FROM relation_edges WHERE id = :id AND user_id = :uid"),
        {"id": edge_id, "uid": user_id},
    )
    mark_dirty(conn, user_id, [ends[0], ends[1]])
    return True


def prune_edges(conn: Connection, user_id: int, edge_ids: Iterable[int]) -> int:
    """Borra aristas en lote por id, capturando todos sus extremos ANTES del DELETE y marcándolos
    dirty. Devuelve cuántas borró."""
    ids = list(dict.fromkeys(edge_ids))
    if not ids:
        return 0
    rows = (
        conn.execute(
            text(
                "SELECT src_slug, src_id, dst_slug, dst_id FROM relation_edges "
                "WHERE user_id = :uid AND id = ANY(:ids)"
            ),
            {"uid": user_id, "ids": ids},
        )
        .mappings()
        .all()
    )
    seeds: set[Ref] = set()
    for r in rows:
        seeds.add(Ref(str(r["src_slug"]), int(r["src_id"])))
        seeds.add(Ref(str(r["dst_slug"]), int(r["dst_id"])))
    n = conn.execute(
        text("DELETE FROM relation_edges WHERE user_id = :uid AND id = ANY(:ids)"),
        {"uid": user_id, "ids": ids},
    ).rowcount
    mark_dirty(conn, user_id, seeds)
    return int(n)


def reject_override(
    conn: Connection,
    user_id: int,
    edge_id: int,
    *,
    provenance: str = PROVENANCE_EXTRACTED,
    evidence: str | None = None,
    relation: str | None = None,
) -> bool:
    """Rechazo por OVERRIDE del humano: marca `rejected` SALTÁNDOSE la monotonía de `resolve_edge`
    (el dueño puede rechazar incluso una `confirmed`; es una aserción suya, no del LLM, de ahí
    `provenance='extracted'` por defecto) y marca dirty los dos extremos. Idempotente: una ya
    rechazada no cambia."""
    ends = _edge_endpoints(conn, user_id, edge_id)
    if ends is None:
        return False
    n = conn.execute(
        text(
            """
            UPDATE relation_edges
            SET verdict = :rej, provenance = :prov, decided_at = NOW(), dirty = TRUE,
                evidence = COALESCE(NULLIF(:ev, ''), evidence),
                relation = COALESCE(NULLIF(:rel, ''), relation)
            WHERE id = :id AND user_id = :uid AND verdict <> :rej
            """
        ),
        {
            "rej": VERDICT_REJECTED,
            "prov": provenance,
            "ev": evidence or "",
            "rel": relation or "",
            "id": edge_id,
            "uid": user_id,
        },
    ).rowcount
    if n > 0:
        mark_dirty(conn, user_id, [ends[0], ends[1]])
    return n > 0


# --- VÉRTICES ---------------------------------------------------------------------------- #
def add_vertex(conn: Connection, user_id: int, ref: Ref) -> None:
    """Marca dirty un vértice RECIÉN creado para que el mantenedor lo evalúe de entrada. Solo el
    vértice (`hops=0`): aún no tiene vecinos. El alta de la fila `mod_*` la hace el módulo dueño;
    acá solo se avisa al grafo, en el mismo tx."""
    mark_dirty(conn, user_id, [ref], hops=0)


def update_vertex(conn: Connection, user_id: int, ref: Ref) -> None:
    """Marca dirty un vértice cuyo dato cambió (label / estado / alias) MÁS sus vecinos: el label
    viejo viajaba en las evaluaciones del vecindario (gate alias-aware, clustering)."""
    mark_dirty(conn, user_id, [ref])


def _cluster_comembers(conn: Connection, user_id: int, ref: Ref) -> set[Ref]:
    """Los demás miembros de los cúmulos a los que pertenece `ref` (vía `relation_cluster_members`).
    La membresía de cúmulo es un vecindario que el marcado por aristas no siempre alcanza a 1 salto;
    al fusionar/borrar conviene reconsiderar a los co-miembros."""
    rows = (
        conn.execute(
            text(
                """
                SELECT DISTINCT m2.member_slug AS slug, m2.member_id AS id
                FROM relation_cluster_members m1
                JOIN relation_cluster_members m2 ON m2.cluster_id = m1.cluster_id
                WHERE m1.user_id = :uid AND m1.member_slug = :slug AND m1.member_id = :id
                  AND NOT (m2.member_slug = :slug AND m2.member_id = :id)
                """
            ),
            {"uid": user_id, "slug": ref.slug, "id": ref.id},
        )
        .mappings()
        .all()
    )
    return {Ref(str(r["slug"]), int(r["id"])) for r in rows}


# --- FUSIÓN / BORRADO de vértices (capturan el alcance ANTES de mutar) ------------------- #
def merge_vertices(conn: Connection, user_id: int, *, absorbed: Ref, survivor: Ref) -> None:
    """Funde `absorbed` en `survivor` a nivel GRAFO: re-apunta sus aristas y su membresía de
    cúmulos al superviviente (colapsa self-loops y duplicados de la UNIQUE lógica) y marca dirty
    al superviviente + ex-vecinos del absorbido + co-miembros de cúmulo, capturados ANTES de
    re-apuntar. NO toca la fila `mod_*` del módulo dueño (eso es dominio del módulo). Mismo slug."""
    if absorbed == survivor or absorbed.slug != survivor.slug:
        return
    p = {"u": user_id, "slug": absorbed.slug, "absb": absorbed.id, "surv": survivor.id}
    seeds: set[Ref] = {survivor}
    for e in edges_touching(conn, user_id, absorbed):
        other = e.dst if e.src == absorbed else e.src
        if other != absorbed:
            seeds.add(other)
    seeds |= _cluster_comembers(conn, user_id, absorbed)

    # Aristas: borrar self-loops, luego las que colisionarían con la UNIQUE lógica del superviviente
    # (src y dst por separado), luego re-apuntar el resto. (Misma disciplina que el merge previo.)
    conn.execute(
        text(
            """
            DELETE FROM relation_edges
            WHERE user_id = :u
              AND ((src_slug = :slug AND src_id = :absb AND dst_slug = :slug AND dst_id = :surv)
                OR (src_slug = :slug AND src_id = :surv AND dst_slug = :slug AND dst_id = :absb))
            """
        ),
        p,
    )
    conn.execute(
        text(
            """
            DELETE FROM relation_edges a
            WHERE a.user_id = :u AND a.src_slug = :slug AND a.src_id = :absb AND EXISTS (
              SELECT 1 FROM relation_edges s
              WHERE s.user_id = :u AND s.src_slug = :slug AND s.src_id = :surv
                AND s.dst_slug = a.dst_slug AND s.dst_id = a.dst_id
                AND s.relation_type = a.relation_type AND s.producer = a.producer)
            """
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE relation_edges SET src_id = :surv "
            "WHERE user_id = :u AND src_slug = :slug AND src_id = :absb"
        ),
        p,
    )
    conn.execute(
        text(
            """
            DELETE FROM relation_edges a
            WHERE a.user_id = :u AND a.dst_slug = :slug AND a.dst_id = :absb AND EXISTS (
              SELECT 1 FROM relation_edges s
              WHERE s.user_id = :u AND s.dst_slug = :slug AND s.dst_id = :surv
                AND s.src_slug = a.src_slug AND s.src_id = a.src_id
                AND s.relation_type = a.relation_type AND s.producer = a.producer)
            """
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE relation_edges SET dst_id = :surv "
            "WHERE user_id = :u AND dst_slug = :slug AND dst_id = :absb"
        ),
        p,
    )

    # Membresía de cúmulos: si el superviviente ya es miembro del mismo cúmulo gana su fila; si no,
    # se re-apunta. (Respeta la UNIQUE de la membresía.)
    conn.execute(
        text(
            """
            DELETE FROM relation_cluster_members a
            WHERE a.user_id = :u AND a.member_slug = :slug AND a.member_id = :absb AND EXISTS (
              SELECT 1 FROM relation_cluster_members s
              WHERE s.cluster_id = a.cluster_id
                AND s.member_slug = :slug AND s.member_id = :surv)
            """
        ),
        p,
    )
    conn.execute(
        text(
            "UPDATE relation_cluster_members SET member_id = :surv "
            "WHERE user_id = :u AND member_slug = :slug AND member_id = :absb"
        ),
        p,
    )

    mark_dirty(conn, user_id, seeds)


def delete_vertex(conn: Connection, user_id: int, ref: Ref) -> None:
    """Borra del GRAFO un vértice que desaparece: elimina sus aristas (quedarían colgando) y su
    membresía de cúmulos, y marca dirty a sus vecinos + co-miembros (capturados ANTES del borrado:
    perdieron una conexión y deben re-evaluarse). NO borra la fila `mod_*` (eso lo hace el módulo
    dueño en el mismo tx)."""
    seeds: set[Ref] = set()
    edge_ids: list[int] = []
    for e in edges_touching(conn, user_id, ref):
        edge_ids.append(e.id)
        other = e.dst if e.src == ref else e.src
        if other != ref:
            seeds.add(other)
    seeds |= _cluster_comembers(conn, user_id, ref)
    if edge_ids:
        conn.execute(
            text("DELETE FROM relation_edges WHERE user_id = :uid AND id = ANY(:ids)"),
            {"uid": user_id, "ids": edge_ids},
        )
    conn.execute(
        text(
            "DELETE FROM relation_cluster_members "
            "WHERE user_id = :uid AND member_slug = :slug AND member_id = :id"
        ),
        {"uid": user_id, "slug": ref.slug, "id": ref.id},
    )
    mark_dirty(conn, user_id, seeds)
