"""Veredictos del gate (`relevance_verdicts`) + el filtro que aplican los worksets.

El veredicto es el CURSOR del gate: una fila por mensaje (UNIQUE inbox_id); la AUSENCIA de
fila = pendiente-de-gate. Con el gate encendido, un correo sin veredicto `relevant` NO entra
a los worksets de resumen/extracción (`workset_gate_clause`). El override manual canónico
sigue siendo `relevance_marks` (0049) y GANA siempre sobre el veredicto, en ambos sentidos:
mark TRUE deja pasar un `not_relevant`, mark FALSE bloquea un `relevant`.

`resolve_insufficient` es la salida de la cola de revisión manual: escribe la mark Y
actualiza el veredicto a `method='manual'` en la MISMA transacción (la mark es lo que ya
consume `quality.relevance`; el update mantiene el cursor del gate coherente).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, text

from memex.core.deadletter import STAGE_RELEVANCE, not_in_review_sql
from memex.core.media import MAX_OCR_ATTEMPTS, MEDIA_NOT_TERMINAL_SQL
from memex.core.relevance_marks import set_mark
from memex.core.source import SourceKind
from memex.db import connection
from memex.processing.windows import WorkRow
from memex.relevance.settings import get_settings
from memex.sources import kind_for_type, kind_types

#: Tipos de source cuya categoría es EMAIL (imap/outlook/...): el alcance del gate. Derivado
#: del registry (no hardcodeado) para incluir tipos push-only futuros automáticamente.
EMAIL_TYPES: list[str] = [t for t in kind_types() if kind_for_type(t) is SourceKind.EMAIL]

VERDICTS = ("relevant", "not_relevant", "insufficient")


@dataclass(frozen=True)
class VerdictItem:
    """Un veredicto a persistir para un mensaje."""

    inbox_id: int
    verdict: str
    method: str  # 'rule' | 'llm' | 'manual'
    rule_id: int | None = None
    reason: str = ""
    model: str | None = None
    mode: str | None = None


def insert_verdicts(conn: Connection, user_id: int, items: list[VerdictItem]) -> int:
    """Inserta veredictos idempotente (ON CONFLICT DO NOTHING): un veredicto existente —
    incluido uno manual— nunca se pisa. Devuelve cuántos se insertaron de verdad."""
    if not items:
        return 0
    inserted = 0
    for it in items:
        n = conn.execute(
            text(
                """
                INSERT INTO relevance_verdicts
                    (user_id, inbox_id, verdict, method, rule_id, reason, model, mode)
                VALUES (:uid, :iid, :verdict, :method, :rule_id, :reason, :model, :mode)
                ON CONFLICT (inbox_id) DO NOTHING
                """
            ),
            {
                "uid": user_id,
                "iid": it.inbox_id,
                "verdict": it.verdict,
                "method": it.method,
                "rule_id": it.rule_id,
                "reason": it.reason[:1000],
                "model": it.model,
                "mode": it.mode,
            },
        ).rowcount
        inserted += n
    return inserted


def clear_verdicts(
    conn: Connection, user_id: int, inbox_ids: list[int], *, keep_manual: bool = True
) -> int:
    """Borra veredictos de los targets (reproceso con `force`). `keep_manual` conserva los
    resueltos a mano (el juicio del dueño no se pisa por un re-run)."""
    manual_filter = " AND method <> 'manual'" if keep_manual else ""
    return int(
        conn.execute(
            text(
                "DELETE FROM relevance_verdicts "
                f"WHERE user_id = :uid AND inbox_id = ANY(:iids){manual_filter}"
            ),
            {"uid": user_id, "iids": inbox_ids},
        ).rowcount
    )


def resolve_insufficient(
    conn: Connection,
    *,
    user_id: int,
    inbox_id: int,
    is_relevant: bool,
    reason: str | None = None,
) -> bool:
    """Resuelve un `insufficient` de la cola de revisión: mark manual + veredicto → manual.

    Ambas escrituras en la MISMA tx del caller. False si el mensaje no tiene un veredicto
    `insufficient` de este usuario (nada que resolver).
    """
    updated = conn.execute(
        text(
            """
            UPDATE relevance_verdicts
            SET verdict = :verdict, method = 'manual', reason = COALESCE(:reason, reason),
                updated_at = NOW()
            WHERE inbox_id = :iid AND user_id = :uid AND verdict = 'insufficient'
            RETURNING id
            """
        ),
        {
            "verdict": "relevant" if is_relevant else "not_relevant",
            "reason": reason,
            "iid": inbox_id,
            "uid": user_id,
        },
    ).first()
    if updated is None:
        return False
    set_mark(conn, user_id=user_id, inbox_id=inbox_id, is_relevant=is_relevant, reason=reason)
    return True


def list_review_queue(conn: Connection, user_id: int, *, limit: int = 100) -> list[dict[str, Any]]:
    """Cola de revisión manual: mensajes con veredicto `insufficient`, más viejos primero.

    Trae lo necesario para decidir sin abrir /datos: remitente, asunto y un snippet del body.
    """
    rows = (
        conn.execute(
            text(
                """
                SELECT rv.inbox_id, rv.reason, rv.created_at, i.occurred_at,
                       i.payload->'from'->>'email' AS from_email,
                       i.payload->>'subject' AS subject,
                       left(COALESCE(i.payload->>'body_text', ''), 280) AS snippet
                FROM relevance_verdicts rv
                JOIN inbox i ON i.id = rv.inbox_id
                WHERE rv.user_id = :uid AND rv.verdict = 'insufficient'
                ORDER BY rv.created_at, rv.inbox_id
                LIMIT :limit
                """
            ),
            {"uid": user_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def workset_gate_clause(conn: Connection, user_id: int) -> tuple[str, dict[str, Any]]:
    """Cláusula AND para los worksets de summarize/extract (requiere alias `i` y `s`).

    Gate apagado → ("", {}): comportamiento actual intacto. Encendido → SOLO para correos
    (`s.type` en EMAIL_TYPES) exige relevancia efectiva: mark manual si existe, si no
    veredicto `relevant`; sin veredicto (pendiente-de-gate) → bloqueado. Otras categorías
    (chat/social/...) pasan siempre.
    """
    if not get_settings(conn, user_id).enabled:
        return "", {}
    clause = """
        AND ( s.type <> ALL(:gate_email_types)
              OR COALESCE(
                   (SELECT rm.is_relevant FROM relevance_marks rm WHERE rm.inbox_id = i.id),
                   (SELECT rv.verdict = 'relevant'
                    FROM relevance_verdicts rv WHERE rv.inbox_id = i.id),
                   FALSE) )
    """
    return clause, {"gate_email_types": EMAIL_TYPES}


def workset_tier_clause(conn: Connection, user_id: int) -> tuple[str, dict[str, Any]]:
    """Cláusula AND de TIER para los worksets de summarize/extract (requiere alias `c`).

    Gate APAGADO (default) → excluye `blacklist` como siempre: las cabeceras de bulk son un
    corte barato y el comportamiento previo queda intacto (cost-safe: apagar el gate NO inunda
    el procesamiento de newsletters). Gate ENCENDIDO → ("", {}): el tier deja de excluir («ser
    masivo no dice nada sobre la relevancia, es solo una señal»); quién entra lo decide la
    relevancia efectiva (`workset_gate_clause`), y el tier queda solo como dial de costo.
    """
    if get_settings(conn, user_id).enabled:
        return "", {}
    return "AND c.tier IN ('batch', 'individual')", {}


def _coerce_payload(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def load_gate_workset(
    user_id: int,
    *,
    source_id: int | None = None,
    limit: int = 200,
    inbox_ids: list[int] | None = None,
) -> list[WorkRow]:
    """Correos clasificados PENDIENTES de gate: sin veredicto y sin mark manual.

    El gate juzga TODOS los correos clasificados, incluido el tier `blacklist`: «ser masivo no
    dice nada sobre la relevancia, es solo una señal» — un newsletter que toca un interés se
    rescata acá (antes ni llegaba). Solo corre con el gate encendido (`run_relevance_gate`
    early-returns apagado), así que no hay costo en frío salvo cuando el dueño lo prende.
    Gates de media (no juzgar antes de que el OCR esté terminal — el texto de las imágenes puede
    ser la señal) y dead-letter propio (`stage='relevance'`). `inbox_ids` acota a un set
    explícito (etapa de reproceso).
    """
    params: dict[str, Any] = {
        "uid": user_id,
        "limit": limit,
        "ocrmax": MAX_OCR_ATTEMPTS,
        "dl_stage": STAGE_RELEVANCE,
        "email_types": EMAIL_TYPES,
    }
    filters = ""
    if source_id is not None:
        filters += " AND i.source_id = :sid"
        params["sid"] = source_id
    if inbox_ids is not None:
        filters += " AND i.id = ANY(:iids)"
        params["iids"] = inbox_ids

    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    f"""
                    SELECT i.id, i.source_id, i.occurred_at, i.payload, c.tier,
                           s.type AS source_type, COALESCE(ma.ocr_text, '') AS ocr_text
                    FROM classifications c
                    JOIN inbox i   ON i.id = c.inbox_id
                    JOIN sources s ON s.id = i.source_id
                    LEFT JOIN (
                        SELECT inbox_id, string_agg(ocr_text, E'\n' ORDER BY id) AS ocr_text
                        FROM media_assets
                        WHERE ocr_status = 'ok' AND ocr_text IS NOT NULL AND ocr_text <> ''
                        GROUP BY inbox_id
                    ) ma ON ma.inbox_id = i.id
                    WHERE c.user_id = :uid
                      AND s.type = ANY(:email_types)
                      AND NOT EXISTS (
                          SELECT 1 FROM relevance_verdicts rv WHERE rv.inbox_id = i.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM relevance_marks rm WHERE rm.inbox_id = i.id
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM media_assets m
                          WHERE m.inbox_id = i.id AND {MEDIA_NOT_TERMINAL_SQL}
                      )
                      AND {not_in_review_sql("i.id")}
                      {filters}
                    ORDER BY i.source_id, i.occurred_at
                    LIMIT :limit
                    """
                ),
                params,
            )
            .mappings()
            .all()
        )

    return [
        WorkRow(
            inbox_id=int(r["id"]),
            source_id=int(r["source_id"]),
            occurred_at=r["occurred_at"],
            payload=_coerce_payload(r["payload"]),
            tier=str(r["tier"]),
            source_type=str(r["source_type"]),
            ocr_text=str(r["ocr_text"]),
        )
        for r in rows
    ]
