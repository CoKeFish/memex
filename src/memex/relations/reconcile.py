"""Reconciliación de cúmulos entre corridas: aparea las comunidades detectadas con los cúmulos
persistidos para que un cúmulo VALIDADO conserve su id/nombre/veredicto aunque su membresía derive.

El problema: Louvain re-corre sobre todo el grafo y devuelve una partición nueva cada vez; sin
reconciliar, cada corrida re-crearía y re-validaría todo. Solución: match codicioso por **Jaccard**
(solape de membresía) contra lo persistido —
- match con MISMA firma → solo refresca `last_seen` (presente, sin cambios);
- match con DERIVA → `sync_members` (preserva podados) + en un confirmed marca `needs_revalidation`
  solo si la deriva pasó el umbral `stable_jaccard` (deriva chica NO re-valida → costo acotado);
- detectado sin match → candidato nuevo, salvo que caiga en un memo de rechazo;
- persistido sin match → candidate se borra; confirmed se disuelve (grace=0, default). El camino de
  gracia (grace>0 → `stale`/`miss_count`/revival) es un slice posterior.

Determinista: el match codicioso ordena por `(jaccard desc, idx detectado, cluster_id)`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import Settings, settings
from memex.logging import get_logger
from memex.relations.cluster_store import (
    ACTIVE_STATUSES,
    StoredCluster,
    delete_cluster,
    insert_candidate,
    load_clusters,
    load_rejected_memos,
    mark_dissolved,
    set_needs_revalidation,
    sync_members,
    touch_last_seen,
)
from memex.relations.clustering import CandidateCluster, cluster_user
from memex.relations.edges import Ref

_log = get_logger("memex.relations.reconcile")


@dataclass
class ReconcileStats:
    """Resumen de una reconciliación."""

    detected: int = 0
    matched_same: int = 0  # match con firma idéntica (touch)
    matched_drift: int = 0  # match con deriva de membresía (sync)
    new_candidates: int = 0  # detectados sin match → candidato nuevo
    memo_skipped: int = 0  # detectados suprimidos por memo de rechazo
    deleted: int = 0  # candidatos persistidos que ya no se detectan → borrados
    dissolved: int = 0  # confirmados sin match → disueltos


def _jaccard(a: frozenset[Ref], b: frozenset[Ref]) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def _is_rejected_memo(
    cluster: CandidateCluster, rejected: list[tuple[str, frozenset[Ref]]], cfg: Settings
) -> bool:
    """¿El detectado es (casi) lo mismo que algo que el LLM ya rechazó? Firma exacta o Jaccard alto
    contra un memo de rechazo."""
    for sig, members in rejected:
        if cluster.signature == sig:
            return True
        if _jaccard(cluster.member_set, members) >= cfg.cluster_reject_memo_jaccard:
            return True
    return False


def _greedy_match(
    detected: list[CandidateCluster], persisted: list[StoredCluster], cfg: Settings
) -> dict[int, int]:
    """Match codicioso 1-a-1 por Jaccard ≥ `match_jaccard` (inclusivo). Devuelve `idx_detectado →
    cluster_id`. Orden determinista: `(jaccard desc, idx detectado, cluster_id)`."""
    candidates: list[tuple[float, int, int]] = []
    for di, d in enumerate(detected):
        for p in persisted:
            j = _jaccard(d.member_set, p.members)
            if j >= cfg.cluster_match_jaccard:
                candidates.append((j, di, p.id))
    candidates.sort(key=lambda t: (-t[0], t[1], t[2]))
    matched_det: dict[int, int] = {}
    used_clusters: set[int] = set()
    for _j, di, cid in candidates:
        if di in matched_det or cid in used_clusters:
            continue
        matched_det[di] = cid
        used_clusters.add(cid)
    return matched_det


def reconcile_clusters(
    conn: Connection,
    user_id: int,
    detected: list[CandidateCluster],
    cfg: Settings | None = None,
    run_id: str | None = None,
) -> ReconcileStats:
    """Reconcilia las comunidades `detected` contra los cúmulos persistidos del user. Idempotente:
    re-detectar la misma partición no cambia nada (todo matchea por firma)."""
    cfg = cfg or settings
    stats = ReconcileStats(detected=len(detected))
    persisted = load_clusters(conn, user_id, ACTIVE_STATUSES)
    rejected = load_rejected_memos(conn, user_id)
    pers_by_id = {p.id: p for p in persisted}

    matched_det = _greedy_match(detected, persisted, cfg)
    matched_clusters = set(matched_det.values())

    # Detectados CON match.
    for di, cid in matched_det.items():
        d = detected[di]
        p = pers_by_id[cid]
        if d.signature == p.signature:
            touch_last_seen(conn, cid)
            stats.matched_same += 1
            continue
        sync_members(conn, user_id, cid, d.member_set, d.signature)
        stats.matched_drift += 1
        if p.status == "confirmed":
            j = _jaccard(d.member_set, p.members)
            set_needs_revalidation(conn, cid, j < cfg.cluster_stable_jaccard)

    # Detectados SIN match → candidato nuevo (salvo memo de rechazo).
    for di, d in enumerate(detected):
        if di in matched_det:
            continue
        if _is_rejected_memo(d, rejected, cfg):
            stats.memo_skipped += 1
            continue
        if insert_candidate(conn, user_id, d, run_id) is None:
            stats.memo_skipped += 1  # chocó con el índice (memo exacto / candidato existente)
        else:
            stats.new_candidates += 1

    # Persistidos SIN match → candidate se borra; confirmed se disuelve (grace=0, default).
    for p in persisted:
        if p.id in matched_clusters:
            continue
        if p.status == "candidate":
            delete_cluster(conn, p.id)
            stats.deleted += 1
        else:  # confirmed / stale: desapareció del grafo (grace>0/stale = slice posterior)
            mark_dissolved(conn, user_id, p.id)
            stats.dissolved += 1

    _log.info(
        "relation.cluster.reconcile",
        user_id=user_id,
        detected=stats.detected,
        matched_same=stats.matched_same,
        matched_drift=stats.matched_drift,
        new_candidates=stats.new_candidates,
        memo_skipped=stats.memo_skipped,
        deleted=stats.deleted,
        dissolved=stats.dissolved,
    )
    return stats


def detect_and_reconcile(
    conn: Connection, user_id: int, cfg: Settings | None = None, run_id: str | None = None
) -> ReconcileStats:
    """Detecta los cúmulos del user y los reconcilia contra lo persistido (un paso, misma tx). Sin
    LLM. `run_id` etiqueta la corrida (se genera si no se pasa)."""
    cfg = cfg or settings
    # Serializa detect+reconcile por-user: los endpoints API son concurrentes (el scheduler corre en
    # serie). Lock por-tx (se libera al cerrar); no-op para una sola conexión.
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('graph_cluster'), (:u)::int)"), {"u": user_id}
    )
    clusters = cluster_user(conn, user_id, cfg)
    return reconcile_clusters(conn, user_id, clusters, cfg, run_id or uuid.uuid4().hex)
