import json
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    ExtractResponse,
    InboxList,
    InboxRow,
    InboxStats,
    ProcessResponse,
    StatsBySource,
    SummarizeResponse,
)
from memex.classifier.rules import classify
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
    where: list[str] = ["user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id, "limit": limit}

    if source_id is not None:
        where.append("source_id = :sid")
        params["sid"] = source_id
    if since is not None:
        where.append("occurred_at >= :since")
        params["since"] = since
    if until is not None:
        where.append("occurred_at < :until")
        params["until"] = until
    if processed == "true":
        where.append("processed_at IS NOT NULL")
    elif processed == "false":
        where.append("processed_at IS NULL")
    if cursor is not None:
        where.append("id > :cur")
        params["cur"] = cursor

    sql = f"""
        SELECT id, source_id, external_id, occurred_at, received_at,
               payload, processed_at, process_error, attempts
        FROM inbox
        WHERE {" AND ".join(where)}
        ORDER BY id
        LIMIT :limit
    """
    with connection() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    items = [dict(r) for r in rows]
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
                    SELECT amount, currency, category, merchant, occurred_on, evidence
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
                    SELECT purpose, model, prompt_tokens, completion_tokens,
                           cost_usd, latency_ms, status, created_at, metadata
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
