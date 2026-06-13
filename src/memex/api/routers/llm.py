"""Selección de proveedor+modelo LLM por consumidor (`llm_consumer_settings`).

Superficie API de la fábrica `memex.llm.registry.build_llm_client`: lista los consumidores
válidos + los proveedores + las filas configuradas del usuario, y permite fijar
provider/model/codex_model/fallback por consumidor (upsert parcial). Espejo del CLI `memex-llm`.
NO dispara LLM — solo configura qué cliente construirá cada consumer cuando corra.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from memex.api.auth import current_user_id
from memex.db import connection
from memex.llm.settings import (
    LLM_CONSUMERS,
    LLM_PROVIDERS,
    LLMConsumerSettings,
    list_consumer_settings,
    upsert_consumer_settings,
)

router = APIRouter(prefix="/llm", tags=["llm"])

UserID = Annotated[int, Depends(current_user_id)]


class LLMConsumerConfig(BaseModel):
    """La config resuelta de UN consumer (provider primario + modelo + cadena de fallback)."""

    consumer: str
    provider: str
    model: str | None
    codex_model: str | None
    fallback: list[str]


class LLMConsumersResponse(BaseModel):
    """Claves válidas + proveedores + las filas que el usuario ya configuró (las ausentes usan
    el default global, o el hardcode DeepSeek si tampoco hay fila `default`)."""

    consumers: list[str]
    providers: list[str]
    configured: list[LLMConsumerConfig]


class LLMConsumerPatch(BaseModel):
    """Upsert parcial: solo los campos no-None se aplican. `model=""`/`codex_model=""` limpian
    el override (vuelven al default del proveedor); `fallback=[]` borra la cadena."""

    provider: str | None = None
    model: str | None = None
    codex_model: str | None = None
    fallback: list[str] | None = None


def _to_config(consumer: str, s: LLMConsumerSettings) -> LLMConsumerConfig:
    return LLMConsumerConfig(
        consumer=consumer,
        provider=s.provider,
        model=s.model,
        codex_model=s.codex_model,
        fallback=list(s.fallback),
    )


@router.get("/consumers", response_model=LLMConsumersResponse)
def get_consumers(user_id: UserID) -> LLMConsumersResponse:
    with connection() as conn:
        rows = list_consumer_settings(conn, user_id)
    return LLMConsumersResponse(
        consumers=list(LLM_CONSUMERS),
        providers=list(LLM_PROVIDERS),
        configured=[_to_config(c, s) for c, s in rows.items()],
    )


@router.patch("/consumers/{consumer}", response_model=LLMConsumerConfig)
def patch_consumer(consumer: str, body: LLMConsumerPatch, user_id: UserID) -> LLMConsumerConfig:
    with connection() as conn:
        try:
            resolved = upsert_consumer_settings(
                conn,
                user_id,
                consumer,
                provider=body.provider,
                model=body.model,
                codex_model=body.codex_model,
                fallback=body.fallback,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
    return _to_config(consumer, resolved)
