"""Superficie API del sistema UNIFICADO de relevancia (`memex.relevance`): el portero que corre
ANTES de resumen/extracción (solo correos).

Acá vive todo el sistema: el GATE (settings, intereses, reglas con dry-run auditado + reversible,
cola de revisión manual, lazo de sugerencia de intereses) Y la capa de SEÑALES fusionada desde el
ex-`/quality` (remitentes rankeados por relevancia, dial de costo por tier batch/individual, y la
cola de candidatos por procedimiento + re-evaluación por el MOTOR ÚNICO — el juez del gate, no un
segundo juez). La corrida del gate NO se dispara desde acá: va por /processing o el CLI;
`/relevance/rules/mine` e `/relevance/interests/mine` sí son on-demand (1 llamada LLM acotada).
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from memex.api.auth import current_user_id
from memex.api.schemas import (
    CandidateStatusRequest,
    GateRuleCreateRequest,
    GateRuleInfo,
    GateRuleList,
    GateRulePatch,
    InterestCreateRequest,
    InterestInfo,
    InterestList,
    InterestPatch,
    InterestSuggestion,
    InterestSuggestionList,
    MineInterestsResponse,
    MineRulesResponse,
    ReevaluateRequest,
    ReevaluateResponse,
    RelevanceCandidate,
    RelevanceCandidateList,
    RelevanceGateSettings,
    RelevanceGateSettingsPatch,
    RelevanceReviewList,
    RelevanceReviewResolveRequest,
    ResolveSuggestionRequest,
    SenderRelevanceList,
    SenderTierInfo,
    SenderTierList,
    SenderTierRequest,
)
from memex.core.sender_tiers import clear_override, list_overrides, set_override
from memex.db import connection
from memex.llm import LLMConfigError, LLMQuotaError
from memex.logging import get_logger
from memex.relevance.candidates import (
    InvalidStatusError,
    list_candidates,
    reevaluate_candidate,
    set_candidate_status,
)
from memex.relevance.interest_mining import (
    list_suggestions,
    resolve_suggestion,
    run_interest_mining,
)
from memex.relevance.interests import (
    create_interest,
    delete_interest,
    list_interests,
    update_interest,
)
from memex.relevance.mining import run_rule_mining, run_rule_mining_cycle
from memex.relevance.rules import create_rule, dry_run_rule, list_rules, set_rule_status
from memex.relevance.settings import GateSettings, get_settings, upsert_settings
from memex.relevance.signals import senders_by_relevance
from memex.relevance.verdicts import list_review_queue, resolve_insufficient

router = APIRouter(prefix="/relevance", tags=["relevance"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.relevance")


# --- Settings ---------------------------------------------------------------------- #


def _settings_payload(s: GateSettings) -> dict[str, Any]:
    return {
        "enabled": s.enabled,
        "mode": s.mode,
        "model": s.model,
        "mining_min_messages": s.mining_min_messages,
        "provider": s.provider,
        "codex_model": s.codex_model,
    }


@router.get("/settings", response_model=RelevanceGateSettings)
async def get_gate_settings(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        s = get_settings(conn, user_id)
    return _settings_payload(s)


@router.patch("/settings", response_model=RelevanceGateSettings)
async def patch_gate_settings(user_id: UserID, body: RelevanceGateSettingsPatch) -> dict[str, Any]:
    """Upsert parcial (solo los campos presentes en el body)."""
    fields = body.model_dump(exclude_unset=True)
    with connection() as conn:
        s = upsert_settings(conn, user_id, **fields)
    _log.info("relevance.settings.patch", user_id=user_id, **fields)
    return _settings_payload(s)


# --- Intereses --------------------------------------------------------------------- #


@router.get("/interests", response_model=InterestList)
async def list_interests_endpoint(user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        return {"items": list_interests(conn, user_id)}


@router.post("/interests", response_model=InterestInfo, status_code=201)
async def create_interest_endpoint(user_id: UserID, body: InterestCreateRequest) -> dict[str, Any]:
    """Alta de un interés. 409 si ya existe; 422 si el texto es vacío."""
    try:
        with connection() as conn:
            row = create_interest(conn, user_id, body.text)
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="ya existe un interés con ese texto") from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    _log.info("relevance.interest.created", user_id=user_id, interest_id=row["id"])
    return row


@router.patch("/interests/{interest_id}", response_model=InterestInfo)
async def patch_interest_endpoint(
    interest_id: int, user_id: UserID, body: InterestPatch
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    try:
        with connection() as conn:
            row = update_interest(
                conn,
                interest_id,
                user_id,
                text_value=fields.get("text"),
                enabled=fields.get("enabled"),
            )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if row is None:
        raise HTTPException(status_code=404, detail="interés no encontrado")
    return row


@router.delete("/interests/{interest_id}", status_code=204)
async def delete_interest_endpoint(interest_id: int, user_id: UserID) -> None:
    with connection() as conn:
        ok = delete_interest(conn, interest_id, user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="interés no encontrado")


# --- Reglas ------------------------------------------------------------------------ #


@router.get("/rules", response_model=GateRuleList)
async def list_rules_endpoint(
    user_id: UserID,
    status: Annotated[Literal["active", "disabled", "rejected", "all"], Query()] = "all",
    effect: Annotated[Literal["block", "allow", "all"], Query()] = "all",
) -> dict[str, Any]:
    with connection() as conn:
        return {
            "items": list_rules(
                conn,
                user_id,
                status=None if status == "all" else status,
                effect=None if effect == "all" else effect,
            )
        }


@router.post("/rules", response_model=GateRuleInfo, status_code=201)
async def create_rule_endpoint(user_id: UserID, body: GateRuleCreateRequest) -> dict[str, Any]:
    """Alta MANUAL de una regla compuesta: corre el dry run primero. Si no pasa (atraparía un correo
    del lado contrario a su polaridad) NO se persiste → 422 con el reporte en `detail` (a
    diferencia de la minería LLM, que sí persiste sus rechazadas como auditoría). 409 si ya existe;
    422 si los predicados son inválidos."""
    with connection() as conn:
        try:
            report = dry_run_rule(
                conn,
                user_id,
                effect=body.effect,
                sender_kind=body.sender_kind,
                sender_value=body.sender_value,
                pattern=body.pattern,
                match_field=body.match_field,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if not report.passes:
            raise HTTPException(status_code=422, detail=report.as_dict())
        row = create_rule(
            conn,
            user_id,
            effect=body.effect,
            sender_kind=body.sender_kind,
            sender_value=body.sender_value,
            pattern=body.pattern,
            match_field=body.match_field,
            proposed_by="manual",
            report=report,
            rationale=body.rationale,
        )
    if row is None:
        raise HTTPException(status_code=409, detail="ya existe una regla con esos predicados")
    _log.info(
        "relevance.rule.created",
        user_id=user_id,
        rule_id=row["id"],
        effect=body.effect,
        sender_kind=body.sender_kind,
        sender_value=body.sender_value,
        pattern=body.pattern,
        match_field=body.match_field,
    )
    return row


@router.patch("/rules/{rule_id}", response_model=GateRuleInfo)
async def patch_rule_endpoint(rule_id: int, user_id: UserID, body: GateRulePatch) -> dict[str, Any]:
    """Toggle reversible active↔disabled. 404 si no existe o está `rejected` (no activable)."""
    with connection() as conn:
        row = set_rule_status(conn, rule_id, user_id, body.status)
    if row is None:
        raise HTTPException(status_code=404, detail="regla no encontrada o rechazada")
    _log.info("relevance.rule.status", user_id=user_id, rule_id=rule_id, status=body.status)
    return row


@router.post("/rules/mine", response_model=MineRulesResponse)
async def mine_rules_endpoint(
    user_id: UserID,
    effect: Annotated[Literal["block", "allow", "all"], Query()] = "all",
) -> dict[str, Any]:
    """Minería on-demand: 1 llamada LLM por polaridad sobre el agregado por remitente + dry run por
    propuesta. `effect=all` (default) mina block y allow. 422 si el gate está apagado o el LLM no
    está configurado; 402 sin saldo."""
    with connection() as conn:
        if not get_settings(conn, user_id).enabled:
            raise HTTPException(status_code=422, detail="el gate de relevancia está apagado")
    try:
        stats = (
            await run_rule_mining_cycle(user_id)
            if effect == "all"
            else await run_rule_mining(user_id, effect=effect)
        )
    except LLMConfigError as e:
        raise HTTPException(status_code=422, detail="LLM no configurado (ANTHROPIC_API_KEY)") from e
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    return {
        "senders": stats.senders,
        "proposed": stats.proposed,
        "activated": stats.activated,
        "rejected": stats.rejected,
        "skipped": stats.skipped,
        "cost_usd": float(stats.cost.total.cost_usd),
    }


# --- Cola de revisión manual ------------------------------------------------------- #


@router.get("/review", response_model=RelevanceReviewList)
async def list_review_endpoint(
    user_id: UserID, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> dict[str, Any]:
    """Correos con veredicto `insufficient` esperando decisión humana (más viejos primero)."""
    with connection() as conn:
        return {"items": list_review_queue(conn, user_id, limit=limit)}


@router.post("/review/{inbox_id}/resolve", status_code=204)
async def resolve_review_endpoint(
    inbox_id: int, user_id: UserID, body: RelevanceReviewResolveRequest
) -> None:
    """Resuelve un `insufficient`: mark manual + veredicto→manual en una tx. 404 si el mensaje
    no tiene un veredicto `insufficient` pendiente."""
    with connection() as conn:
        ok = resolve_insufficient(
            conn,
            user_id=user_id,
            inbox_id=inbox_id,
            is_relevant=body.is_relevant,
            reason=body.reason,
        )
    if not ok:
        raise HTTPException(status_code=404, detail="sin veredicto insufficient pendiente")
    _log.info(
        "relevance.review.resolved",
        user_id=user_id,
        inbox_id=inbox_id,
        is_relevant=body.is_relevant,
    )


# --- Lazo de intereses (rechazo manual → sugerir editar intereses) ----------------- #


@router.get("/interests/suggestions", response_model=InterestSuggestionList)
async def list_interest_suggestions_endpoint(
    user_id: UserID,
    status: Annotated[Literal["proposed", "accepted", "rejected", "all"], Query()] = "proposed",
) -> dict[str, Any]:
    """Sugerencias de editar intereses (pendientes por default). `all` = todos los estados."""
    with connection() as conn:
        items = list_suggestions(conn, user_id=user_id, status=None if status == "all" else status)
    return {"items": items}


@router.post("/interests/mine", response_model=MineInterestsResponse)
async def mine_interests_endpoint(user_id: UserID) -> dict[str, Any]:
    """Mina sugerencias de interés on-demand desde las marcas manuales (1 llamada LLM).

    422 si el gate está apagado o el LLM no está configurado; 402 sin saldo. Umbral-gated: con
    pocas marcas devuelve `proposed=0` sin llamar al LLM.
    """
    with connection() as conn:
        if not get_settings(conn, user_id).enabled:
            raise HTTPException(status_code=422, detail="el gate de relevancia está apagado")
    try:
        stats = await run_interest_mining(user_id)
    except LLMConfigError as e:
        raise HTTPException(status_code=422, detail="LLM no configurado (ANTHROPIC_API_KEY)") from e
    except LLMQuotaError as e:
        raise HTTPException(status_code=402, detail="saldo LLM agotado") from e
    return {
        "marks": stats.marks,
        "proposed": stats.proposed,
        "inserted": stats.inserted,
        "cost_usd": float(stats.cost.total.cost_usd),
    }


@router.post("/interests/suggestions/{suggestion_id}/resolve", response_model=InterestSuggestion)
async def resolve_interest_suggestion_endpoint(
    suggestion_id: int, user_id: UserID, body: ResolveSuggestionRequest
) -> dict[str, Any]:
    """Acepta (aplica el alta/baja del interés) o descarta una sugerencia. 404 si ya no está."""
    with connection() as conn:
        row = resolve_suggestion(
            conn, user_id=user_id, suggestion_id=suggestion_id, accept=body.accept
        )
    if row is None:
        raise HTTPException(status_code=404, detail="sugerencia no encontrada o ya resuelta")
    _log.info(
        "relevance.interest_suggestion.resolved",
        user_id=user_id,
        suggestion_id=suggestion_id,
        accept=body.accept,
    )
    return row


# --- Señales: remitentes y candidatos (capa ex-/quality, fusionada acá) ------------ #


@router.get("/senders", response_model=SenderRelevanceList)
async def list_sender_relevance(
    user_id: UserID,
    source_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> dict[str, Any]:
    """Remitentes rankeados por relevancia (ruido primero). `source_id` acota a una fuente."""
    with connection() as conn:
        items = senders_by_relevance(conn, user_id=user_id, limit=limit, source_id=source_id)
    _log.info("relevance.senders", user_id=user_id, rows=len(items), source_id=source_id)
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
    _log.info("relevance.sender_tier.set", user_id=user_id, tier=body.tier, email=body.sender_email)
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
    _log.info("relevance.sender_tiers.list", user_id=user_id, rows=len(items))
    return {"items": items}


@router.get("/candidates", response_model=RelevanceCandidateList)
async def list_candidates_endpoint(
    user_id: UserID,
    status: Annotated[Literal["open", "confirmed", "dismissed", "all"], Query()] = "open",
    procedure: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """Candidatos a (re)evaluar detectados por los procedimientos (ruido primero).

    `all` = todos los estados; `procedure` filtra por procedimiento (ej. `fact_count`).
    """
    with connection() as conn:
        items = list_candidates(
            conn,
            user_id=user_id,
            status=None if status == "all" else status,
            procedure=procedure,
        )
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
