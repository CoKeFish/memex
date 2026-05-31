"""Dead-letter de los workers LLM (summarize/extract): contador de fallos por mensaje.

Gap (c) de la auditoría de errores LLM. El cursor de esos workers es la AUSENCIA de fila de
completitud (summary_inbox_links / module_extractions) → un fallo se reintenta en cada corrida.
Bien para fallos transitorios; mal para un mensaje 'veneno' que falla SIEMPRE (reintento infinito,
costo por llamada cada vez). Acá se lleva el contador: al alcanzar `MAX_WORK_ATTEMPTS` el mensaje
pasa a 'review' ('pendiente de revisión') y los worksets lo excluyen — sin descartarlo en silencio.

Calca el patrón de `media_assets.ocr_attempts` + MAX_OCR_ATTEMPTS del worker de OCR (que ya tenía
dead-letter), pero la unidad de trabajo de summarize/extract es la VENTANA (varios mensajes por
llamada), así que el contador es por (stage, inbox_id): cuando una ventana falla se suma 1 a CADA
mensaje suyo. El 402/saldo NO pasa por acá (aborta la corrida vía LLMQuotaError, no se cuenta).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger

_log = get_logger("memex.core.deadletter")

#: Etapas que usan dead-letter (deben coincidir con el CHECK de la migración 0012).
STAGE_SUMMARIZE = "summarize"
STAGE_EXTRACT = "extract"

#: Fallos (sin éxito intermedio que cursoree el mensaje fuera del work-set) antes de mandarlo a
#: 'pendiente de revisión'. Espeja MAX_OCR_ATTEMPTS. Llegar a este nº = veneno (falla determinista).
MAX_WORK_ATTEMPTS = 3


def not_in_review_sql(inbox_ref: str) -> str:
    """Fragmento SQL para el WHERE de un workset: el mensaje `inbox_ref` (p. ej. ``i.id``) NO está
    en 'review' para la etapa `:dl_stage`. El caller DEBE pasar el bind param ``dl_stage``."""
    return (
        "NOT EXISTS (SELECT 1 FROM work_item_failures wf "
        f"WHERE wf.inbox_id = {inbox_ref} AND wf.stage = :dl_stage AND wf.status = 'review')"
    )


def record_failures(user_id: int, stage: str, inbox_ids: Sequence[int], error: str) -> None:
    """Suma 1 al contador de fallos de cada `inbox_id` (UPSERT por (stage, inbox_id)).

    Al alcanzar `MAX_WORK_ATTEMPTS`, marca status='review' → los worksets dejan de reintentarlo.
    Tx propia (como record_llm_call): no participa de la transacción del worker. Los que recién
    cruzan a 'review' se loguean (`deadletter.review`) para post-mortem (ADR-007).
    """
    if not inbox_ids:
        return
    newly_review: list[int] = []
    with connection() as conn:
        for iid in inbox_ids:
            row = conn.execute(
                text(
                    """
                    INSERT INTO work_item_failures
                        (user_id, stage, inbox_id, attempts, last_error, status)
                    VALUES (:uid, :stage, :iid, 1, :err, 'failing')
                    ON CONFLICT (stage, inbox_id) DO UPDATE SET
                        attempts   = work_item_failures.attempts + 1,
                        last_error = excluded.last_error,
                        status     = CASE WHEN work_item_failures.attempts + 1 >= :maxatt
                                          THEN 'review' ELSE 'failing' END,
                        updated_at = NOW()
                    RETURNING status, attempts
                    """
                ),
                {
                    "uid": user_id,
                    "stage": stage,
                    "iid": iid,
                    "err": error[:1000],
                    "maxatt": MAX_WORK_ATTEMPTS,
                },
            ).first()
            if row is not None and row[0] == "review" and int(row[1]) == MAX_WORK_ATTEMPTS:
                newly_review.append(iid)
    if newly_review:
        _log.warning(
            "deadletter.review",
            stage=stage,
            inbox_ids=newly_review,
            attempts=MAX_WORK_ATTEMPTS,
        )


def list_review(user_id: int, stage: str) -> list[dict[str, object]]:
    """Mensajes en 'pendiente de revisión' (dead-letter) de una etapa, más recientes primero."""
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT inbox_id, attempts, last_error, updated_at
                    FROM work_item_failures
                    WHERE user_id = :uid AND stage = :stage AND status = 'review'
                    ORDER BY updated_at DESC
                    """
                ),
                {"uid": user_id, "stage": stage},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def requeue(user_id: int, stage: str, inbox_id: int) -> bool:
    """Saca un mensaje de revisión (borra su fila) → vuelve al work-set. True si existía."""
    with connection() as conn:
        result = conn.execute(
            text(
                "DELETE FROM work_item_failures "
                "WHERE user_id = :uid AND stage = :stage AND inbox_id = :iid"
            ),
            {"uid": user_id, "stage": stage, "iid": inbox_id},
        )
    return result.rowcount > 0
