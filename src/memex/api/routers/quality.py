"""Sistema de calidad: relevancia por remitente (lectura determinista, read-only).

`GET /quality/senders` — remitentes rankeados por relevancia para domar ruido (ruido primero). Sin
LLM, sin mutación, acotado al dueño. La marca manual, la acción "no procesar"/descartar y la cola de
candidatos llegan en fases posteriores; esta es la vista que las habilita.
"""

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.api.schemas import (
    CandidateStatusRequest,
    JudgeRequest,
    JudgeResponse,
    RelevanceCandidate,
    RelevanceCandidateList,
    SenderDiscardRequest,
    SenderDiscardResponse,
    SenderRelevanceList,
    SenderTierInfo,
    SenderTierRequest,
)
from memex.core.filters import create_rule
from memex.core.sender_tiers import clear_override, set_override
from memex.db import connection
from memex.llm import LLMConfigError, LLMQuotaError
from memex.logging import get_logger
from memex.quality.candidates import InvalidStatusError, list_candidates, set_candidate_status
from memex.quality.judge_llm import JudgeUnavailableError, judge_sender
from memex.quality.relevance import senders_by_relevance

router = APIRouter(prefix="/quality", tags=["quality"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.quality")


@router.get("/senders", response_model=SenderRelevanceList)
async def list_sender_relevance(
    user_id: UserID,
    source_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """Remitentes rankeados por relevancia (ruido primero). `source_id` acota a una fuente."""
    with connection() as conn:
        items = senders_by_relevance(conn, user_id=user_id, limit=limit, source_id=source_id)
    _log.info("quality.senders", user_id=user_id, rows=len(items), source_id=source_id)
    return {"items": items}


@router.post("/senders/tier", response_model=SenderTierInfo)
async def set_sender_tier_endpoint(user_id: UserID, body: SenderTierRequest) -> dict[str, Any]:
    """No procesar: fuerza el tier de los mensajes futuros de un remitente (típico: blacklist).

    Conserva los mensajes en inbox (no borra); el classifier usa este tier en vez de la heurística.
    Prospectivo (no re-clasifica lo ya clasificado). Acción asistida: la confirma el usuario.
    """
    with connection() as conn:
        row = set_override(
            conn,
            user_id=user_id,
            sender_email=body.sender_email,
            tier=body.tier,
            reason=body.reason,
        )
    _log.info("quality.sender_tier.set", user_id=user_id, tier=body.tier, email=body.sender_email)
    return row


@router.delete("/senders/tier", status_code=204)
async def clear_sender_tier_endpoint(
    user_id: UserID, sender_email: Annotated[str, Query()]
) -> None:
    """Quita el override de tier de un remitente (vuelve a la heurística). 404 si no existía."""
    with connection() as conn:
        ok = clear_override(conn, user_id=user_id, sender_email=sender_email)
    if not ok:
        raise HTTPException(status_code=404, detail="sin override")


@router.post("/senders/discard", response_model=SenderDiscardResponse)
async def discard_sender_endpoint(user_id: UserID, body: SenderDiscardRequest) -> dict[str, Any]:
    """Descartar: crea una regla filter_rules ignore para el remitente (drop puro = basura clara).

    Los mensajes futuros se filtran antes de guardarse. Idempotente: reusa una regla equivalente.
    Reversible desde /filtros.
    """
    email = body.sender_email.strip().lower()
    scope = {"from.email": {"equals": email}}
    with connection() as conn:
        existing = conn.execute(
            text(
                "SELECT id FROM filter_rules "
                "WHERE user_id = :uid AND action = 'ignore' AND scope = CAST(:scope AS JSONB)"
            ),
            {"uid": user_id, "scope": json.dumps(scope)},
        ).scalar()
        if existing is not None:
            return {"rule_id": int(existing), "created": False}
        rule_id = create_rule(
            conn,
            user_id=user_id,
            source_type=None,
            source_id=None,
            scope=scope,
            action="ignore",
            priority=200,
        )
    _log.info("quality.sender_discard", user_id=user_id, sender=email, rule_id=rule_id)
    return {"rule_id": rule_id, "created": True}


@router.get("/candidates", response_model=RelevanceCandidateList)
async def list_candidates_endpoint(
    user_id: UserID,
    status: Annotated[Literal["open", "confirmed", "dismissed", "all"], Query()] = "open",
) -> dict[str, Any]:
    """Candidatos a filtrar detectados por el job (ruido primero). `all` = todos los estados."""
    with connection() as conn:
        items = list_candidates(conn, user_id=user_id, status=None if status == "all" else status)
    return {"items": items}


@router.post("/candidates/status", response_model=RelevanceCandidate)
async def set_candidate_status_endpoint(
    user_id: UserID, body: CandidateStatusRequest
) -> dict[str, Any]:
    """Mueve el estado de un candidato (confirmed/dismissed). 404 si no existe."""
    try:
        with connection() as conn:
            row = set_candidate_status(
                conn, user_id=user_id, sender_key=body.sender_key, status=body.status
            )
    except InvalidStatusError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="candidato no encontrado")
    return row


@router.post("/candidates/judge", response_model=JudgeResponse)
async def judge_candidate_endpoint(user_id: UserID, body: JudgeRequest) -> dict[str, Any]:
    """Juez LLM de relevancia (zona gris) para un candidato — ADVISORY, no acciona. Gateado por
    MEMEX_QUALITY_LLM; 422 si está apagado, 404 si el candidato no existe o no tiene muestra."""
    try:
        verdict = await judge_sender(user_id, body.sender_key)
    except JudgeUnavailableError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except LLMConfigError as e:
        raise HTTPException(status_code=422, detail="LLM no configurado (DEEPSEEK_API_KEY)") from e
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    if verdict is None:
        raise HTTPException(status_code=404, detail="candidato sin muestra o inexistente")
    return {
        "is_relevant": verdict.is_relevant,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
    }
