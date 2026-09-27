"""Re-marca la FASE 1 (determinista, sin LLM) sobre las transacciones YA cargadas.

Las reglas de la FASE 1 corren al insertar cada fila. Cuando cambian —el veto por mismo origen y
el bonus por fuentes independientes (2026-09-12)— lo ya cargado queda marcado con la regla vieja:
pares que hoy serían veto siguen como candidatos (y bloquean la consolidación de sus filas), y
pares que hoy entrarían no existen. Este módulo recomputa los pares con la regla ACTUAL sobre todas
las filas del usuario y reconcilia la tabla de candidatos:

- pares que la regla ya no produce y NADIE decidió (`candidate`, o `confirmed` por `procedural`)
  → se borran;
- pares nuevos → se insertan igual que al insertar (candidato o auto-confirmado);
- candidatos vigentes cuyo reason/score/estado cambió → se actualizan;
- lo decidido por el juez (`decided_by` distinto de procedural) se RESPETA: una decisión pagada
  no se re-deriva, exista o no el par bajo la regla nueva.

Luego re-marca `processing_outcome` de las filas tocadas (en par → `pending`; sin par → `unique`),
igual que al insertar. La FASE 2 y la consolidación siguen siendo pasos aparte (`memex-finance
dedup`, `memex-finance consolidate` o el ciclo `finance` del scheduler). Compara todas contra todas
en memoria: lineal en pares, sobra para el volumen de una persona (cientos a pocos miles).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger
from memex.modules.finance.dedup import DedupRow, mark_duplicates
from memex.modules.finance.module import insert_pairs, load_dedup_rows

_log = get_logger(__name__)

#: Quien «decide» un auto-confirmado. Todo otro `decided_by` es una decisión que se respeta.
_PROCEDURAL = "procedural"


@dataclass(frozen=True)
class PairChange:
    """Un par que entra, sale o cambia, con lo necesario para reportarlo."""

    a_id: int
    b_id: int
    reason: str
    score: float
    status: str  # estado resultante ('candidate' | 'confirmed'), o el que tenía si se quita


@dataclass
class RemarkStats:
    rows: int = 0
    pairs_now: int = 0
    added: list[PairChange] = field(default_factory=list)
    removed: list[PairChange] = field(default_factory=list)
    updated: int = 0
    #: Pares que la regla ya no produce pero el juez decidió: se dejan como están.
    kept_decided: int = 0
    dry_run: bool = False
    #: Las filas leídas, por id (para que el reporte muestre monto/contraparte/fecha).
    rows_by_id: dict[int, DedupRow] = field(default_factory=dict)


def remark_phase1(user_id: int, *, dry_run: bool = False) -> RemarkStats:
    """Re-marca en su propia transacción (commitea al salir; `dry_run` no escribe nada)."""
    with connection() as conn:
        return remark_phase1_in(conn, user_id, dry_run=dry_run)


def remark_phase1_in(conn: Connection, user_id: int, *, dry_run: bool = False) -> RemarkStats:
    rows = load_dedup_rows(conn, user_id)
    pairs = {(p.a_id, p.b_id): p for p in mark_duplicates(rows, [])}
    stats = RemarkStats(
        rows=len(rows),
        pairs_now=len(pairs),
        dry_run=dry_run,
        rows_by_id={r.transaction_id: r for r in rows},
    )
    existing = (
        conn.execute(
            text(
                "SELECT transaction_a_id AS a, transaction_b_id AS b, status, decided_by, reason, "
                "score FROM mod_finance_dedup_candidates WHERE user_id = :uid"
            ),
            {"uid": user_id},
        )
        .mappings()
        .all()
    )

    to_delete: list[tuple[int, int]] = []
    to_update: list[dict[str, object]] = []
    seen: set[tuple[int, int]] = set()
    for e in existing:
        key = (int(e["a"]), int(e["b"]))
        seen.add(key)
        decided = e["decided_by"] is not None and str(e["decided_by"]) != _PROCEDURAL
        now = pairs.get(key)
        if now is None:
            if decided:
                stats.kept_decided += 1
            else:
                to_delete.append(key)
                stats.removed.append(
                    PairChange(
                        key[0],
                        key[1],
                        str(e["reason"]),
                        float(e["score"] or 0),
                        str(e["status"]),
                    )
                )
            continue
        if decided:
            continue  # la regla lo sigue produciendo y el juez ya decidió: nada que tocar
        new_status = "confirmed" if now.decision == "confirmed" else "candidate"
        current = (str(e["reason"]), float(e["score"] or 0), str(e["status"]))
        if current != (now.reason, now.score, new_status):
            to_update.append(
                {
                    "uid": user_id,
                    "a": key[0],
                    "b": key[1],
                    "reason": now.reason,
                    "score": now.score,
                    "status": new_status,
                    "decided_by": _PROCEDURAL if new_status == "confirmed" else None,
                }
            )
            stats.updated += 1
    to_insert = [p for k, p in pairs.items() if k not in seen]
    stats.added = [
        PairChange(
            p.a_id,
            p.b_id,
            p.reason,
            p.score,
            "confirmed" if p.decision == "confirmed" else "candidate",
        )
        for p in to_insert
    ]

    _log.info(
        "finance.remark",
        user_id=user_id,
        rows=stats.rows,
        pairs_now=stats.pairs_now,
        added=len(stats.added),
        removed=len(stats.removed),
        updated=stats.updated,
        kept_decided=stats.kept_decided,
        dry_run=dry_run,
    )
    if dry_run:
        return stats

    if to_delete:
        conn.execute(
            text(
                "DELETE FROM mod_finance_dedup_candidates WHERE user_id = :uid "
                "AND transaction_a_id = :a AND transaction_b_id = :b"
            ),
            [{"uid": user_id, "a": a, "b": b} for a, b in to_delete],
        )
    if to_update:
        conn.execute(
            text(
                """
                UPDATE mod_finance_dedup_candidates
                   SET reason = :reason, score = :score, status = :status,
                       decided_by = :decided_by,
                       decided_at = CASE WHEN CAST(:decided_by AS TEXT) IS NULL THEN NULL
                                         ELSE NOW() END
                 WHERE user_id = :uid AND transaction_a_id = :a AND transaction_b_id = :b
                """
            ),
            to_update,
        )
    if to_insert:
        insert_pairs(conn, user_id, to_insert)

    touched: set[int] = set()
    for a, b in to_delete:
        touched.update((a, b))
    for p in to_insert:
        touched.update((p.a_id, p.b_id))
    if touched:
        still_paired: set[int] = set()
        for a, b in conn.execute(
            text(
                "SELECT transaction_a_id, transaction_b_id FROM mod_finance_dedup_candidates "
                "WHERE user_id = :uid AND status IN ('candidate', 'confirmed')"
            ),
            {"uid": user_id},
        ).all():
            still_paired.update((int(a), int(b)))
        pending = sorted(t for t in touched if t in still_paired)
        unique = sorted(t for t in touched if t not in still_paired)
        for outcome, ids in (("pending", pending), ("unique", unique)):
            if ids:
                conn.execute(
                    text(
                        "UPDATE mod_finance_transactions SET processed_at = NOW(), "
                        "processing_outcome = :o WHERE user_id = :uid AND id = ANY(:ids)"
                    ),
                    {"o": outcome, "uid": user_id, "ids": ids},
                )
    return stats
