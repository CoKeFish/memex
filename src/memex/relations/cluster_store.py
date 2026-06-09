"""Persistencia de los cúmulos (`relation_clusters` / `relation_cluster_members`).

Capa de acceso a datos para la reconciliación (`reconcile`) y el validador (`clusters_llm`). NO
decide nada: lee/escribe filas. Disciplina:
- La `signature` y los sets que devuelve incluyen TODOS los miembros (incl. `pruned`) = el set
  DETECTADO (la detección no conoce la poda; el Jaccard debe ser apples-to-apples).
- `sync_members` es un SET-DIFF que PRESERVA las filas sobrevivientes (y su flag `pruned`): inserta
  los nuevos `pruned=FALSE`, borra los idos, deja intactos los que siguen — así un outlier podado en
  un confirmado que deriva no resucita.
- `insert_candidate` es idempotente vía el índice único PARCIAL `(user_id, signature) WHERE status
  IN ('candidate','rejected')` (predicado en el `ON CONFLICT`, NO `ON CONSTRAINT`): re-detectar no
  duplica y un memo de rechazo bloquea la re-propuesta exacta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.relations.clustering import CandidateCluster
from memex.relations.edges import (
    CUMULO_SLUG,
    PRODUCER_LLM,
    RELTYPE_MIEMBRO_DE,
    STATUS_CONFIRMED,
    Ref,
    propose_edge,
)

#: Estados activos (participan del match de reconciliación).
ACTIVE_STATUSES: tuple[str, ...] = ("candidate", "confirmed", "stale")


@dataclass(frozen=True)
class StoredCluster:
    """Un cúmulo persistido + su membresía (el set detectado, incl. podados)."""

    id: int
    status: str
    name: str
    description: str
    confidence: Decimal | None
    member_count: int
    signature: str
    validated_signature: str | None
    has_confirmed_edge: bool
    needs_revalidation: bool
    miss_count: int
    members: frozenset[Ref] = field(default_factory=frozenset)
    pruned: frozenset[Ref] = field(default_factory=frozenset)

    @property
    def live_members(self) -> frozenset[Ref]:
        """Los miembros NO podados (los que proyectan arista `miembro_de`)."""
        return self.members - self.pruned


def _load_members(conn: Connection, cluster_ids: list[int]) -> dict[int, tuple[set[Ref], set[Ref]]]:
    """`cluster_id → (todos, podados)` de los cúmulos dados (una query)."""
    out: dict[int, tuple[set[Ref], set[Ref]]] = {cid: (set(), set()) for cid in cluster_ids}
    if not cluster_ids:
        return out
    for r in conn.execute(
        text(
            "SELECT cluster_id, member_slug, member_id, pruned FROM relation_cluster_members "
            "WHERE cluster_id = ANY(:ids)"
        ),
        {"ids": cluster_ids},
    ).mappings():
        ref = Ref(str(r["member_slug"]), int(r["member_id"]))
        allm, pruned = out[int(r["cluster_id"])]
        allm.add(ref)
        if r["pruned"]:
            pruned.add(ref)
    return out


def _row_to_cluster(r: object, members: set[Ref], pruned: set[Ref]) -> StoredCluster:
    m = r  # mapping
    return StoredCluster(
        id=int(m["id"]),  # type: ignore[index]
        status=str(m["status"]),  # type: ignore[index]
        name=str(m["name"]),  # type: ignore[index]
        description=str(m["description"]),  # type: ignore[index]
        confidence=m["confidence"],  # type: ignore[index]
        member_count=int(m["member_count"]),  # type: ignore[index]
        signature=str(m["signature"]),  # type: ignore[index]
        validated_signature=(
            str(m["validated_signature"]) if m["validated_signature"] is not None else None  # type: ignore[index]
        ),
        has_confirmed_edge=bool(m["has_confirmed_edge"]),  # type: ignore[index]
        needs_revalidation=bool(m["needs_revalidation"]),  # type: ignore[index]
        miss_count=int(m["miss_count"]),  # type: ignore[index]
        members=frozenset(members),
        pruned=frozenset(pruned),
    )


def load_clusters(
    conn: Connection, user_id: int, statuses: tuple[str, ...] = ACTIVE_STATUSES
) -> list[StoredCluster]:
    """Los cúmulos del user con `status ∈ statuses`, con su membresía cargada."""
    rows = (
        conn.execute(
            text(
                "SELECT * FROM relation_clusters WHERE user_id = :u AND status = ANY(:st) "
                "ORDER BY id"
            ),
            {"u": user_id, "st": list(statuses)},
        )
        .mappings()
        .all()
    )
    members = _load_members(conn, [int(r["id"]) for r in rows])
    out: list[StoredCluster] = []
    for r in rows:
        allm, pruned = members[int(r["id"])]
        out.append(_row_to_cluster(r, allm, pruned))
    return out


def load_rejected_memos(conn: Connection, user_id: int) -> list[tuple[str, frozenset[Ref]]]:
    """`(signature, miembros)` de los cúmulos rechazados (el memo): para suprimir re-propuestas
    cercanas a algo que el LLM ya descartó."""
    return [(c.signature, c.members) for c in load_clusters(conn, user_id, ("rejected",))]


def _insert_members(
    conn: Connection, user_id: int, cluster_id: int, members: frozenset[Ref]
) -> None:
    for ref in members:
        conn.execute(
            text(
                "INSERT INTO relation_cluster_members "
                "(user_id, cluster_id, member_slug, member_id) VALUES (:u, :c, :s, :i) "
                "ON CONFLICT (cluster_id, member_slug, member_id) DO NOTHING"
            ),
            {"u": user_id, "c": cluster_id, "s": ref.slug, "i": ref.id},
        )


def insert_candidate(
    conn: Connection, user_id: int, cluster: CandidateCluster, run_id: str | None = None
) -> int | None:
    """Inserta un cúmulo candidato nuevo (idempotente por el índice único parcial). Devuelve su id,
    o `None` si la firma ya existía como candidate o rejected (memo) → el caller lo saltea."""
    new_id = conn.execute(
        text(
            """
            INSERT INTO relation_clusters
              (user_id, status, signature, member_count, has_confirmed_edge, run_id)
            VALUES (:u, 'candidate', :sig, :mc, :hce, :rid)
            ON CONFLICT (user_id, signature) WHERE status IN ('candidate','rejected') DO NOTHING
            RETURNING id
            """
        ),
        {
            "u": user_id,
            "sig": cluster.signature,
            "mc": len(cluster.members),
            "hce": cluster.has_confirmed_edge,
            "rid": run_id,
        },
    ).scalar()
    if new_id is None:
        return None
    _insert_members(conn, user_id, int(new_id), cluster.member_set)
    return int(new_id)


def touch_last_seen(conn: Connection, cluster_id: int) -> None:
    """Match con la MISMA firma: refresca `last_seen` y resetea `miss_count` (sigue
    presente)."""
    conn.execute(
        text(
            "UPDATE relation_clusters SET last_seen_at = NOW(), miss_count = 0, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"id": cluster_id},
    )


def sync_members(
    conn: Connection, user_id: int, cluster_id: int, new_members: frozenset[Ref], signature: str
) -> None:
    """Match con DERIVA: set-diff que preserva las filas (y flags `pruned`) sobrevivientes.
    Actualiza `signature`/`member_count`/`last_seen` y resetea `miss_count`."""
    existing = {
        Ref(str(r["member_slug"]), int(r["member_id"]))
        for r in conn.execute(
            text(
                "SELECT member_slug, member_id FROM relation_cluster_members WHERE cluster_id = :c"
            ),
            {"c": cluster_id},
        ).mappings()
    }
    to_add = new_members - existing
    to_del = existing - new_members
    _insert_members(conn, user_id, cluster_id, frozenset(to_add))
    for ref in to_del:
        conn.execute(
            text(
                "DELETE FROM relation_cluster_members "
                "WHERE cluster_id = :c AND member_slug = :s AND member_id = :i"
            ),
            {"c": cluster_id, "s": ref.slug, "i": ref.id},
        )
    conn.execute(
        text(
            "UPDATE relation_clusters SET signature = :sig, member_count = :mc, "
            "last_seen_at = NOW(), miss_count = 0, updated_at = NOW() WHERE id = :id"
        ),
        {"sig": signature, "mc": len(new_members), "id": cluster_id},
    )


def set_needs_revalidation(conn: Connection, cluster_id: int, value: bool) -> None:
    """Marca si la membresía derivó lo bastante como para que el validador LLM la re-juzgue."""
    conn.execute(
        text(
            "UPDATE relation_clusters SET needs_revalidation = :v, updated_at = NOW() "
            "WHERE id = :id"
        ),
        {"v": value, "id": cluster_id},
    )


def delete_cluster(conn: Connection, cluster_id: int) -> None:
    """Borra un cúmulo (y su membresía por CASCADE). Para candidatos que ya no se detectan."""
    conn.execute(text("DELETE FROM relation_clusters WHERE id = :id"), {"id": cluster_id})


def _delete_cluster_edges(conn: Connection, user_id: int, cluster_id: int) -> None:
    """Borra las aristas `miembro_de` que apuntan a este cúmulo (no-op hasta materializarlas)."""
    conn.execute(
        text(
            "DELETE FROM relation_edges WHERE user_id = :u AND producer = :p "
            "AND relation_type = :rt AND dst_slug = :cs AND dst_id = :cid"
        ),
        {
            "u": user_id,
            "p": PRODUCER_LLM,
            "rt": RELTYPE_MIEMBRO_DE,
            "cs": CUMULO_SLUG,
            "cid": cluster_id,
        },
    )


def mark_dissolved(conn: Connection, user_id: int, cluster_id: int) -> None:
    """El cúmulo desapareció del grafo: despublica (borra sus aristas `miembro_de`) y lo marca
    `dissolved` (terminal; deja de proyectar el vértice `cumulo`)."""
    _delete_cluster_edges(conn, user_id, cluster_id)
    conn.execute(
        text(
            "UPDATE relation_clusters SET status = 'dissolved', decided_at = NOW(), "
            "updated_at = NOW() WHERE id = :id"
        ),
        {"id": cluster_id},
    )


def reject_cluster(
    conn: Connection,
    user_id: int,
    cluster_id: int,
    signature: str,
    *,
    name: str = "",
    description: str = "",
) -> None:
    """El validador descartó el cúmulo → memo de rechazo. Maneja la colisión del índice parcial: si
    ya existe un memo `rejected` con la misma firma, BORRA esta fila (el memo ya registra el
    rechazo); si no, la transiciona a `rejected`. Despublica sus aristas `miembro_de`."""
    _delete_cluster_edges(conn, user_id, cluster_id)
    existing = conn.execute(
        text(
            "SELECT id FROM relation_clusters "
            "WHERE user_id = :u AND signature = :s AND status = 'rejected' AND id <> :id"
        ),
        {"u": user_id, "s": signature, "id": cluster_id},
    ).scalar()
    if existing is not None:
        delete_cluster(conn, cluster_id)
        return
    conn.execute(
        text(
            "UPDATE relation_clusters SET status = 'rejected', name = :name, description = :desc, "
            "decided_at = NOW(), updated_at = NOW() WHERE id = :id"
        ),
        {"name": name, "desc": description, "id": cluster_id},
    )


def materialize_cluster_edges(conn: Connection, user_id: int) -> int:
    """Materializa (idempotente + GC) las aristas `miembro_de` de los cúmulos confirmados: por cada
    miembro NO podado, una arista `miembro → cumulo`; y BORRA las que sobran (miembros podados o que
    se fueron al derivar la partición — `prune_orphan_edges` NO las atrapa porque el miembro sigue
    siendo un vértice vivo). GC + propose en una pasada. Devuelve cuántas aristas vivas quedaron."""
    n = 0
    for c in load_clusters(conn, user_id, ("confirmed", "stale")):
        live = c.live_members
        existing = {
            Ref(str(r["src_slug"]), int(r["src_id"])): int(r["id"])
            for r in conn.execute(
                text(
                    "SELECT id, src_slug, src_id FROM relation_edges "
                    "WHERE user_id = :u AND producer = :p AND relation_type = :rt "
                    "AND dst_slug = :cs AND dst_id = :cid"
                ),
                {
                    "u": user_id,
                    "p": PRODUCER_LLM,
                    "rt": RELTYPE_MIEMBRO_DE,
                    "cs": CUMULO_SLUG,
                    "cid": c.id,
                },
            ).mappings()
        }
        for ref in live:
            propose_edge(
                conn,
                user_id,
                ref,
                Ref(CUMULO_SLUG, c.id),
                producer=PRODUCER_LLM,
                relation_type=RELTYPE_MIEMBRO_DE,
                status=STATUS_CONFIRMED,
                confidence=c.confidence,
            )
            n += 1
        stale_ids = [eid for ref, eid in existing.items() if ref not in live]
        if stale_ids:
            conn.execute(
                text("DELETE FROM relation_edges WHERE id = ANY(:ids)"), {"ids": stale_ids}
            )
    return n
