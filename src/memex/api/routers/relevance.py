"""Gate de relevancia por intereses personales: settings, intereses, reglas y revisión manual.

Superficie API del módulo `memex.relevance` (el portero que corre ANTES de resumen/extracción,
solo correos). A diferencia del sistema de calidad advisory (`/quality`), este módulo SÍ
acciona: sus reglas bloquean procesamiento — siempre con dry run previo, auditadas (reporte
persistido) y reversibles (PATCH status). La corrida del gate NO se dispara desde acá: va por
las corridas de procesamiento (/processing) o el CLI; `/relevance/rules/mine` sí es on-demand
(1 llamada LLM, barata y acotada).
"""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError

from memex.api.auth import current_user_id
from memex.api.schemas import (
    GateRuleCreateRequest,
    GateRuleInfo,
    GateRuleList,
    GateRulePatch,
    InterestCreateRequest,
    InterestInfo,
    InterestList,
    InterestPatch,
    MineRulesResponse,
    RelevanceGateSettings,
    RelevanceGateSettingsPatch,
    RelevanceReviewList,
    RelevanceReviewResolveRequest,
)
from memex.db import connection
from memex.llm import LLMConfigError, LLMQuotaError
from memex.logging import get_logger
from memex.relevance.interests import (
    create_interest,
    delete_interest,
    list_interests,
    update_interest,
)
from memex.relevance.mining import run_rule_mining
from memex.relevance.rules import create_rule, dry_run_rule, list_rules, set_rule_status
from memex.relevance.settings import GateSettings, get_settings, upsert_settings
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
) -> dict[str, Any]:
    with connection() as conn:
        return {"items": list_rules(conn, user_id, status=None if status == "all" else status)}


@router.post("/rules", response_model=GateRuleInfo, status_code=201)
async def create_rule_endpoint(user_id: UserID, body: GateRuleCreateRequest) -> dict[str, Any]:
    """Alta MANUAL de una regla: corre el dry run primero. Si atraparía algún correo relevante
    NO se persiste → 422 con el reporte en `detail` (a diferencia de la minería LLM, que sí
    persiste sus rechazadas como auditoría). 409 si ya existe."""
    with connection() as conn:
        try:
            report = dry_run_rule(conn, user_id, body.kind, body.pattern)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        if not report.passes:
            raise HTTPException(status_code=422, detail=report.as_dict())
        row = create_rule(
            conn,
            user_id,
            kind=body.kind,
            pattern=body.pattern,
            proposed_by="manual",
            report=report,
            rationale=body.rationale,
        )
    if row is None:
        raise HTTPException(status_code=409, detail="ya existe una regla con ese kind+pattern")
    _log.info(
        "relevance.rule.created",
        user_id=user_id,
        rule_id=row["id"],
        kind=body.kind,
        pattern=body.pattern,
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
async def mine_rules_endpoint(user_id: UserID) -> dict[str, Any]:
    """Minería on-demand: 1 llamada LLM sobre el agregado de no-relevantes + dry run por
    propuesta. 422 si el gate está apagado o el LLM no está configurado; 402 sin saldo."""
    with connection() as conn:
        if not get_settings(conn, user_id).enabled:
            raise HTTPException(status_code=422, detail="el gate de relevancia está apagado")
    try:
        stats = await run_rule_mining(user_id)
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
