"""Endpoint on-demand de transporte: `GET /transport/next-arrival`.

Pregunta "¿llego a mi próximo evento?" a pedido. Solo CONSULTA — no emite avisos (eso lo hace el
daemon). Construye el proveedor de mapas por request (puede gastar una llamada a Maps) y traduce un
fallo del proveedor a 502.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from memex.api.auth import current_user_id
from memex.geo.client import GeoError
from memex.geo.providers import build_provider_from_env
from memex.transport.config import TransportConfig
from memex.transport.service import assess_next_arrival

router = APIRouter(prefix="/transport", tags=["transport"])

UserID = Annotated[int, Depends(current_user_id)]


class NextArrivalResponse(BaseModel):
    """Veredicto de llegada al próximo evento (o `upcoming=False` si no hay ninguno medible)."""

    upcoming: bool
    event_id: int | None = None
    title: str | None = None
    event_start: datetime | None = None
    verdict: str | None = None
    leave_by: datetime | None = None
    travel_seconds: int | None = None
    reason: str | None = None


@router.get("/next-arrival", response_model=NextArrivalResponse)
async def next_arrival(user_id: UserID) -> NextArrivalResponse:
    """¿Llego a tiempo a mi próximo evento? Calcula al vuelo (puede gastar una llamada a Maps)."""
    cfg = TransportConfig.from_env()
    now = datetime.now(cfg.tz)
    try:
        provider = build_provider_from_env()
        try:
            result = await assess_next_arrival(user_id=user_id, provider=provider, cfg=cfg, now=now)
        finally:
            await provider.aclose()
    except GeoError as e:
        raise HTTPException(status_code=502, detail=f"proveedor de mapas: {e}") from e
    if result is None:
        return NextArrivalResponse(upcoming=False)
    assessment = result.assessment
    return NextArrivalResponse(
        upcoming=True,
        event_id=result.event_id,
        title=result.title,
        event_start=result.event_start,
        verdict=assessment.verdict.value,
        leave_by=assessment.leave_by,
        travel_seconds=assessment.travel_seconds,
        reason=result.reason,
    )
