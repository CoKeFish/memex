import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    ClassificationInfo,
    ClassifyRequest,
    ExtractResponse,
    FeedbackInfo,
    FeedbackRequest,
    InboxList,
    InboxRow,
    InboxStats,
    ProcessResponse,
    ReprocessRequest,
    ReprocessResponse,
    StatsBySource,
    SummarizeResponse,
)
from memex.classifier.rules import classify
from memex.core.feedback import InvalidFeedbackError, get_feedback, record_feedback
from memex.db import connection
from memex.logging import bind_request_context, get_logger

router = APIRouter(prefix="/inbox", tags=["inbox"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.inbox")


@router.get("", response_model=InboxList)
async def list_inbox(
    user_id: UserID,
    source_id: int | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    processed: Literal["true", "false", "all"] = "all",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    cursor: int | None = Query(default=None, description="id > cursor for pagination"),
) -> dict[str, Any]:
    where: list[str] = ["i.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if source_id is not None:
        where.append("i.source_id = :sid")
        params["sid"] = source_id
    if since is not None:
        where.append("i.occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("i.occurred_at < :until")
        params["until"] = until
    if processed == "true":
        where.append("i.processed_at IS NOT NULL")
    elif processed == "false":
        where.append("i.processed_at IS NULL")
    if cursor is not None:
        where.append("i.id > :cur")
        params["cur"] = cursor

    # Estado para la lista (índices por inbox_id en las 3 tablas):
    #  - classifications: el tier (blacklist/batch/individual) = "en qué filtro entró".
    #  - summary/extraction (EXISTS): avance real del pipeline. `inbox.processed_at` quedó en desuso
    #    (casi nunca se setea), así que el estado se deriva de clasificación + resumen/extracción.
    sql = f"""
        SELECT i.id, i.source_id, i.external_id, i.occurred_at, i.received_at,
               i.payload, i.processed_at, i.process_error, i.attempts,
               c.tier AS _tier, c.metadata AS _cmeta,
               EXISTS (SELECT 1 FROM summary_inbox_links sl
                       WHERE sl.inbox_id = i.id) AS _summarized,
               EXISTS (SELECT 1 FROM module_extractions me
                       WHERE me.inbox_id = i.id) AS _extracted
        FROM inbox i
        LEFT JOIN classifications c ON c.inbox_id = i.id
        WHERE {" AND ".join(where)}
        ORDER BY i.id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        tier = d.pop("_tier", None)
        cmeta = d.pop("_cmeta", None)
        d["classification"] = {"tier": tier, "metadata": cmeta} if tier else None
        d["summarized"] = bool(d.pop("_summarized", False))
        d["extracted"] = bool(d.pop("_extracted", False))
        items.append(d)
    next_cursor = items[-1]["id"] if len(items) == limit else None
    return {"items": items, "next_cursor": next_cursor}


@router.get("/stats", response_model=InboxStats)
async def stats(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT source_id,
                           COUNT(*) AS total,
                           COUNT(*) FILTER (WHERE processed_at IS NULL) AS pending,
                           COUNT(*) FILTER (WHERE process_error IS NOT NULL) AS errored
                    FROM inbox
                    WHERE user_id = :uid
                    GROUP BY source_id
                    ORDER BY source_id
                    """
                ),
                {"uid": user_id},
            )
            .mappings()
            .all()
        )
    sources = {
        r["source_id"]: StatsBySource(total=r["total"], pending=r["pending"], errored=r["errored"])
        for r in rows
    }
    return {"sources": sources}


@router.get("/{inbox_id}", response_model=InboxRow)
async def get_inbox(inbox_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    """
                    SELECT i.id, i.source_id, i.external_id, i.occurred_at, i.received_at,
                           i.payload, i.processed_at, i.process_error, i.attempts,
                           c.tier AS _tier, c.metadata AS _cmeta
                    FROM inbox i
                    LEFT JOIN classifications c ON c.inbox_id = i.id
                    WHERE i.id = :id AND i.user_id = :uid
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    data = dict(row)
    tier = data.pop("_tier", None)
    cmeta = data.pop("_cmeta", None)
    data["classification"] = {"tier": tier, "metadata": cmeta} if tier else None

    # Resultados de fases posteriores (resumen + extracciones), para el detalle.
    with connection() as conn:
        summary = (
            conn.execute(
                text(
                    """
                    SELECT s.id, s.tier, s.content, s.created_at
                    FROM summaries s
                    JOIN summary_inbox_links sl ON sl.summary_id = s.id
                    WHERE sl.inbox_id = :id AND s.user_id = :uid
                    """
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        finance = (
            conn.execute(
                text(
                    """
                    SELECT amount, currency, category, merchant, occurred_on,
                           description, evidence
                    FROM mod_finance_expenses
                    WHERE user_id = :uid AND :id = ANY(source_inbox_ids)
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "id": inbox_id},
            )
            .mappings()
            .all()
        )
        calendar = (
            conn.execute(
                text(
                    """
                    SELECT title, starts_on, ends_on, start_time, end_time, location, evidence
                    FROM mod_calendar_events
                    WHERE user_id = :uid AND :id = ANY(source_inbox_ids)
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "id": inbox_id},
            )
            .mappings()
            .all()
        )
        # Estado de extracción: el cursor marca "procesado" aunque no haya filas (sin datos).
        ext_modules = (
            conn.execute(
                text(
                    "SELECT DISTINCT module_slug FROM module_extractions "
                    "WHERE inbox_id = :id ORDER BY module_slug"
                ),
                {"id": inbox_id},
            )
            .scalars()
            .all()
        )
        # Traza de llamadas LLM atribuidas a este mensaje (auditoría/costo por correo).
        llm_calls = (
            conn.execute(
                text(
                    """
                    SELECT request_id, purpose, model, prompt_tokens, completion_tokens,
                           cost_usd, latency_ms, status, error_message, created_at, metadata
                    FROM llm_calls
                    WHERE user_id = :uid AND inbox_id = :id
                    ORDER BY created_at
                    """
                ),
                {"uid": user_id, "id": inbox_id},
            )
            .mappings()
            .all()
        )
        # Adjuntos (media_assets): referencia + estado/texto de OCR. El blob va por /media/{id}.
        media = (
            conn.execute(
                text(
                    """
                    SELECT id, sha256, content_type, filename, extension, size_bytes,
                           ocr_status, ocr_model, ocr_text, ocr_error, ocr_attempts, ocr_done_at
                    FROM media_assets
                    WHERE user_id = :uid AND inbox_id = :id
                    ORDER BY id
                    """
                ),
                {"uid": user_id, "id": inbox_id},
            )
            .mappings()
            .all()
        )
        feedback = get_feedback(conn, inbox_id)
    data["summary"] = dict(summary) if summary else None
    data["extraction"] = {
        "done": len(ext_modules) > 0,
        "modules": [str(m) for m in ext_modules],
        "finance": [dict(r) for r in finance],
        "calendar": [dict(r) for r in calendar],
    }
    calls = [dict(c) for c in llm_calls]
    data["llm"] = {
        "calls": len(calls),
        "cost_usd": float(sum(float(c["cost_usd"]) for c in calls)),
        "prompt_tokens": sum(int(c["prompt_tokens"]) for c in calls),
        "completion_tokens": sum(int(c["completion_tokens"]) for c in calls),
        "items": [{**c, "cost_usd": float(c["cost_usd"])} for c in calls],
    }
    data["media"] = [dict(m) for m in media]
    data["feedback"] = feedback
    data["summarized"] = summary is not None
    data["extracted"] = len(ext_modules) > 0
    return data


def _coerce_payload(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}


@router.post("/{inbox_id}/process", response_model=ProcessResponse)
async def process_inbox(inbox_id: int, user_id: UserID) -> dict[str, Any]:
    """Procesa (clasifica) un mensaje puntual de forma determinista — sin LLM.

    Asigna el `tier` (blacklist/batch) según las reglas de `classify()` y lo persiste en
    `classifications` (idempotente por `UNIQUE(inbox_id)`). Si ya estaba clasificado, devuelve
    el tier existente sin re-escribir. Summarize/extract (LLM, por lotes) son otro paso.
    """
    with connection() as conn:
        row = (
            conn.execute(
                text("SELECT payload FROM inbox WHERE id = :id AND user_id = :uid"),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if not row:
            raise HTTPException(status_code=404, detail="not found")
        existing = (
            conn.execute(
                text("SELECT tier, metadata FROM classifications WHERE inbox_id = :id"),
                {"id": inbox_id},
            )
            .mappings()
            .first()
        )
        if existing:
            meta = existing["metadata"] or {}
            reason = str(meta.get("rule", "")) if isinstance(meta, dict) else ""
            return {
                "inbox_id": inbox_id,
                "tier": existing["tier"],
                "reason": reason,
                "classified": False,
                "already": True,
            }
        result = classify(_coerce_payload(row["payload"]))
        conn.execute(
            text(
                """
                INSERT INTO classifications (user_id, inbox_id, tier, metadata)
                VALUES (:uid, :iid, :tier, CAST(:metadata AS JSONB))
                ON CONFLICT (inbox_id) DO NOTHING
                """
            ),
            {
                "uid": user_id,
                "iid": inbox_id,
                "tier": result.tier,
                "metadata": json.dumps(result.metadata),
            },
        )
    _log.info("inbox.processed", user_id=user_id, inbox_id=inbox_id, tier=result.tier)
    return {
        "inbox_id": inbox_id,
        "tier": result.tier,
        "reason": result.reason,
        "classified": True,
        "already": False,
    }


Scope = Annotated[Literal["individual", "window"], Query()]


@router.post("/{inbox_id}/summarize", response_model=SummarizeResponse)
async def summarize_inbox_endpoint(
    inbox_id: int,
    user_id: UserID,
    scope: Scope = "individual",
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Resume (LLM) un mensaje o su ventana. Requiere clasificación previa."""
    from memex.llm import LLMConfigError, LLMError, LLMQuotaError
    from memex.summarizer.worker import InboxNotClassifiedError, summarize_inbox

    bind_request_context(inbox_id=inbox_id)  # atribuye el costo LLM a este mensaje
    try:
        return await summarize_inbox(user_id, inbox_id, scope=scope, force=force)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="not found") from e
    except InboxNotClassifiedError as e:
        raise HTTPException(status_code=409, detail="clasificá el mensaje primero") from e
    except LLMConfigError as e:
        raise HTTPException(status_code=422, detail="LLM no configurado (DEEPSEEK_API_KEY)") from e
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"error de LLM: {e}") from e


@router.post("/{inbox_id}/extract", response_model=ExtractResponse)
async def extract_inbox_endpoint(
    inbox_id: int,
    user_id: UserID,
    scope: Scope = "individual",
    force: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Extrae (módulos finance/calendar, LLM) sobre un mensaje o su ventana. Requiere clasificar."""
    from memex.llm import LLMConfigError, LLMError, LLMQuotaError
    from memex.modules.orchestrator import InboxNotClassifiedError, extract_inbox

    bind_request_context(inbox_id=inbox_id)  # atribuye el costo LLM a este mensaje
    try:
        return await extract_inbox(user_id, inbox_id, scope=scope, force=force)
    except LookupError as e:
        raise HTTPException(status_code=404, detail="not found") from e
    except InboxNotClassifiedError as e:
        raise HTTPException(status_code=409, detail="clasificá el mensaje primero") from e
    except LLMConfigError as e:
        raise HTTPException(status_code=422, detail="LLM no configurado (DEEPSEEK_API_KEY)") from e
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    except LLMError as e:
        raise HTTPException(status_code=502, detail=f"error de LLM: {e}") from e


@router.post("/{inbox_id}/reprocess", response_model=ReprocessResponse)
async def reprocess_inbox_endpoint(
    inbox_id: int, user_id: UserID, body: ReprocessRequest
) -> dict[str, Any]:
    """Re-aplica etapas (media/ocr/classify/summarize/extract) a UN mensaje.

    Síncrono y best-effort por etapa: cada una se corre en orden de dependencia y su resultado (o
    error) viaja en `results[<stage>]`. Los lotes van por el CLI `memex-reprocess`.
    """
    from memex.reprocess import reprocess

    with connection() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM inbox WHERE id = :id AND user_id = :uid"),
            {"id": inbox_id, "uid": user_id},
        ).scalar()
    if not exists:
        raise HTTPException(status_code=404, detail="not found")

    bind_request_context(inbox_id=inbox_id)  # atribuye el costo LLM/OCR a este mensaje
    try:
        return await reprocess(user_id, stages=body.stages, targets=[inbox_id], force=body.force)
    except ValueError as e:  # stages inválidas
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/{inbox_id}/feedback", response_model=FeedbackInfo)
async def feedback_inbox_endpoint(
    inbox_id: int, user_id: UserID, body: FeedbackRequest
) -> dict[str, Any]:
    """Registra feedback rápido del usuario sobre un mensaje (SOLO captura — no corrige nada).

    Guarda las categorías + nota y un snapshot de lo observado (tier/remitente/asunto/adjuntos) para
    que el feedback sea autocontenido al evaluar/calibrar después. Upsert: re-reportar reemplaza.
    """
    try:
        with connection() as conn:
            row = (
                conn.execute(
                    text(
                        """
                        SELECT i.payload->>'subject' AS subject,
                               i.payload->'from'->>'email' AS from_email,
                               c.tier AS tier,
                               EXISTS (SELECT 1 FROM media_assets m WHERE m.inbox_id = i.id)
                                   AS has_media
                        FROM inbox i
                        LEFT JOIN classifications c ON c.inbox_id = i.id
                        WHERE i.id = :id AND i.user_id = :uid
                        """
                    ),
                    {"id": inbox_id, "uid": user_id},
                )
                .mappings()
                .first()
            )
            if not row:
                raise HTTPException(status_code=404, detail="not found")
            snapshot = {
                "tier": row["tier"],
                "from_email": row["from_email"],
                "subject": row["subject"],
                "has_media": bool(row["has_media"]),
            }
            return record_feedback(
                conn,
                user_id=user_id,
                inbox_id=inbox_id,
                kinds=body.kinds,
                note=body.note,
                metadata=snapshot,
            )
    except InvalidFeedbackError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/{inbox_id}/classification", response_model=ClassificationInfo)
async def set_classification_endpoint(
    inbox_id: int, user_id: UserID, body: ClassifyRequest
) -> dict[str, Any]:
    """Override MANUAL del tier de un mensaje, aplicado ya (blacklist/batch/individual).

    Marca la clasificación como `manual` (guarda el tier previo en `metadata.prev_tier`). El worker
    determinista no la pisa (inserta solo si falta); un re-clasificar con `force` sí la recalcula.
    """
    meta: dict[str, Any] = {"rule": "manual", "manual": True, "by": "user"}
    with connection() as conn:
        prev = (
            conn.execute(
                text(
                    "SELECT c.tier FROM inbox i LEFT JOIN classifications c ON c.inbox_id = i.id "
                    "WHERE i.id = :id AND i.user_id = :uid"
                ),
                {"id": inbox_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
        if prev is None:
            raise HTTPException(status_code=404, detail="not found")
        if prev["tier"] is not None:
            meta["prev_tier"] = prev["tier"]
        conn.execute(
            text(
                """
                INSERT INTO classifications (user_id, inbox_id, tier, metadata)
                VALUES (:uid, :iid, :tier, CAST(:meta AS JSONB))
                ON CONFLICT (inbox_id) DO UPDATE
                    SET tier = EXCLUDED.tier, metadata = EXCLUDED.metadata
                """
            ),
            {"uid": user_id, "iid": inbox_id, "tier": body.tier, "meta": json.dumps(meta)},
        )
    _log.info("inbox.classification.manual", user_id=user_id, inbox_id=inbox_id, tier=body.tier)
    return {"tier": body.tier, "metadata": meta}
