"""Sistema de calidad: relevancia por remitente (lectura determinista, read-only).

`GET /quality/senders` — remitentes rankeados por relevancia para domar ruido (ruido primero). Sin
LLM, sin mutación, acotado al dueño. La marca manual, la acción "no procesar"/descartar y la cola de
candidatos llegan en fases posteriores; esta es la vista que las habilita.
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from memex.api.auth import current_user_id
from memex.api.schemas import (
    CandidateStatusRequest,
    ReevaluateRequest,
    ReevaluateResponse,
    RelevanceCandidate,
    RelevanceCandidateList,
    SenderRelevanceList,
    SenderTierInfo,
    SenderTierList,
    SenderTierRequest,
)
from memex.core.sender_tiers import clear_override, list_overrides, set_override
from memex.db import connection
from memex.llm import LLMQuotaError
from memex.logging import get_logger
from memex.quality.candidates import (
    InvalidStatusError,
    list_candidates,
    reevaluate_candidate,
    set_candidate_status,
)
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
    """Dial de COSTO: fuerza el tier (batch/individual) de los mensajes futuros de un remitente.

    Prospectivo (no re-clasifica lo ya clasificado). «No procesar un remitente» ya no es un tier:
    es una regla del gate (`POST /relevance/rules` kind=sender_email).
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


# Path estático: si algún día se agrega GET /senders/{key}, registrarla DESPUÉS de esta ruta.
@router.get("/senders/tiers", response_model=SenderTierList)
async def list_sender_tiers_endpoint(user_id: UserID) -> dict[str, Any]:
    """Overrides de tier por remitente del usuario (gestión en /filtros). Recientes primero."""
    with connection() as conn:
        items = list_overrides(conn, user_id=user_id)
    _log.info("quality.sender_tiers.list", user_id=user_id, rows=len(items))
    return {"items": items}


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
                conn,
                user_id=user_id,
                sender_key=body.sender_key,
                status=body.status,
                procedure=body.procedure,
            )
    except InvalidStatusError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="candidato no encontrado")
    return row


@router.post("/candidates/reevaluate", response_model=ReevaluateResponse)
async def reevaluate_candidate_endpoint(user_id: UserID, body: ReevaluateRequest) -> dict[str, int]:
    """Re-evalúa la muestra de un candidato por el MOTOR ÚNICO (el juez del gate + los intereses).

    No es un segundo juez: corre el gate sobre la muestra (force). 404 si el candidato no existe o
    no tiene muestra; 402 si se agotó el saldo LLM. Con el gate apagado devuelve ceros (no hay
    juez que correr).
    """
    try:
        result = await reevaluate_candidate(
            user_id, sender_key=body.sender_key, procedure=body.procedure
        )
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    if result is None:
        raise HTTPException(status_code=404, detail="candidato sin muestra o inexistente")
    return result
