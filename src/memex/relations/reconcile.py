"""Reconciliación de cúmulos entre corridas (PARTIDOR): decide qué blobs detectados necesitan una
(re)partición del LLM, SIN llamarlo, preservando la identidad de los cúmulos ya particionados.

Modelo: cada cúmulo `confirmed` lleva el `blob_signature` del blob del que salió. Un blob detectado
está MANEJADO si ya hay hijos confirmed/stale con esa firma (o un memo `rejected`): re-detectarlo
igual es ESTABLE (no se re-particiona). Un blob que DERIVÓ (ingesta aditiva) cambia de firma → no
está manejado → candidato a (re)particionar; el partidor lo parte y SINCRONIZA en sitio por Jaccard
los hijos viejos que solapan (la identidad/nombre no se pierde).

La re-evaluación es un paso PERIÓDICO (no por-ingesta): la ingesta solo teje las aristas
deterministas de lo nuevo; el partidor corre cada cierto tiempo. Aditivo: borrar es raro — solo
candidatos viejos que ya no se detectan y hijos cuyos miembros desaparecieron de TODO blob.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import Settings, settings
from memex.logging import get_logger
from memex.relations.cluster_store import (
    blob_partitioned,
    delete_cluster,
    insert_candidate,
    load_clusters,
    mark_dissolved,
    touch_blob,
)
from memex.relations.clustering import CandidateCluster, cluster_user
from memex.relations.edges import Ref

_log = get_logger("memex.relations.reconcile")


@dataclass
class ReconcileStats:
    """Resumen de una reconciliación (sin LLM)."""

    detected: int = 0
    matched_same: int = 0  # blobs ya particionados (estables, sin re-LLM)
    matched_drift: int = 0  # sin uso en el modelo partidor (la deriva crea candidato); queda 0
    new_candidates: int = 0  # blobs nuevos o derivados → a particionar
    memo_skipped: int = 0  # blob suprimido por un candidato existente o un memo de rechazo
    deleted: int = 0  # candidatos viejos cuyo blob ya no se detecta → borrados
    dissolved: int = 0  # hijos cuyos miembros desaparecieron de TODO blob → disueltos


def reconcile_clusters(
    conn: Connection,
    user_id: int,
    detected: list[CandidateCluster],
    cfg: Settings | None = None,
    run_id: str | None = None,
) -> ReconcileStats:
    """Reconcilia los blobs `detected` contra lo persistido. Idempotente: re-detectar la misma
    partición no cambia nada (todo está manejado por `blob_signature`)."""
    cfg = cfg or settings
    stats = ReconcileStats(detected=len(detected))
    detected_sigs = {d.signature for d in detected}
    all_blob_members: set[Ref] = {m for d in detected for m in d.member_set}

    for d in detected:
        if blob_partitioned(conn, user_id, d.signature):
            touch_blob(conn, user_id, d.signature)  # estable (no-op si es un memo de rechazo)
            stats.matched_same += 1
        elif insert_candidate(conn, user_id, d, run_id) is None:
            stats.memo_skipped += 1  # ya hay candidato/memo con esa firma
        else:
            stats.new_candidates += 1

    # Candidatos viejos cuyo blob ya no se detecta (derivó antes de particionarse) → borrar.
    for c in load_clusters(conn, user_id, ("candidate",)):
        if c.signature not in detected_sigs:
            delete_cluster(conn, c.id)
            stats.deleted += 1

    # Hijos confirmed/stale cuyos miembros ya no están en NINGÚN blob → disolver (contexto ido).
    for c in load_clusters(conn, user_id, ("confirmed", "stale")):
        if not (c.live_members & all_blob_members):
            mark_dissolved(conn, user_id, c.id)
            stats.dissolved += 1

    _log.info(
        "relation.cluster.reconcile",
        user_id=user_id,
        detected=stats.detected,
        stable=stats.matched_same,
        new_candidates=stats.new_candidates,
        memo_skipped=stats.memo_skipped,
        deleted=stats.deleted,
        dissolved=stats.dissolved,
    )
    return stats


def detect_and_reconcile(
    conn: Connection, user_id: int, cfg: Settings | None = None, run_id: str | None = None
) -> ReconcileStats:
    """Detecta los blobs y los reconcilia contra lo persistido (un paso, misma tx). Sin LLM.
    `run_id` etiqueta la corrida (se genera si no se pasa)."""
    cfg = cfg or settings
    # Serializa detect+reconcile por-user: los endpoints API son concurrentes (el scheduler corre en
    # serie). Lock por-tx (se libera al cerrar); no-op para una sola conexión.
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('graph_cluster'), (:u)::int)"), {"u": user_id}
    )
    clusters = cluster_user(conn, user_id, cfg)
    return reconcile_clusters(conn, user_id, clusters, cfg, run_id or uuid.uuid4().hex)
