"""Punto único de construcción del cliente LLM por consumidor.

`build_llm_client(consumer)` resuelve provider+model de `llm_consumer_settings` y construye el
cliente concreto (o un `FallbackClient` si hay cadena). Reemplaza el
`DeepSeekClient(LLMConfig.from_env())` que cada worker tenía hardcodeado como default: ahora se
intercambia proveedor por configuración sin tocar código. Los callers siguen tipando contra el
Protocol `LLMClient` y NO conocen al proveedor concreto.

`aclose_llm(client)` cierra cualquier cliente del Protocol que exponga `aclose` (DeepSeek/Anthropic
y `FallbackClient` lo tienen; Codex no —subprocess, sin conexión persistente— → no-op). Reemplaza
el `isinstance(..., DeepSeekClient)` que filtraba el tipo concreto en el cleanup de los workers, y
es lo que permite que la fábrica devuelva Anthropic/Codex/Fallback sin fugar el httpx del cliente.
"""

from __future__ import annotations

from memex.db import connection
from memex.llm.anthropic import AnthropicClient, anthropic_config
from memex.llm.client import LLMClient
from memex.llm.codex import CodexClient, CodexError
from memex.llm.config import LLMConfig, LLMConfigError
from memex.llm.deepseek import DeepSeekClient
from memex.llm.fallback import FallbackClient
from memex.llm.settings import LLMConsumerSettings, get_consumer_settings
from memex.logging import get_logger

_log = get_logger("memex.llm.registry")


def _build_one(provider: str, settings: LLMConsumerSettings) -> LLMClient:
    """Construye el cliente concreto de un proveedor con el modelo de los settings."""
    if provider == "anthropic":
        config = anthropic_config()
        if settings.model:
            config = config.model_copy(update={"default_model": settings.model})
        return AnthropicClient(config)
    if provider == "codex":
        return CodexClient(model=settings.codex_model)
    # deepseek (default y hardcode de último recurso)
    return DeepSeekClient(LLMConfig.from_env(default_model=settings.model))


def build_llm_client(consumer: str, *, user_id: int = 1) -> LLMClient:
    """Cliente default del `consumer` según `llm_consumer_settings`; el caller lo cierra.

    Cadena efectiva = [provider, *fallback]. Codex que no puede construirse (sesión/binario ausente)
    se OMITE de la cadena si hay alternativas; si codex es el único proveedor, propaga el
    `CodexError` accionable (igual que el gate). Cadena vacía o key de DeepSeek/Anthropic faltante
    → el error de construcción propaga (config rota, no transitorio).
    """
    with connection() as conn:
        settings = get_consumer_settings(conn, user_id, consumer)

    chain = [settings.provider, *settings.fallback]
    built: list[tuple[str, LLMClient]] = []
    for provider in chain:
        try:
            built.append((provider, _build_one(provider, settings)))
        except CodexError:
            if len(chain) == 1:
                raise  # codex único → error accionable, no hay a quién caer
            _log.warning("llm.codex.unavailable", consumer=consumer, provider=provider)

    if not built:
        raise LLMConfigError(f"ningún proveedor disponible para consumer {consumer!r}")
    if len(built) == 1:
        return built[0][1]
    return FallbackClient(built)


def build_provider_client(
    provider: str, *, model: str | None = None, codex_model: str | None = None
) -> LLMClient:
    """Cliente de UN proveedor, sin leer la DB ni armar cadena.

    Para overrides por corrida (el flag `--provider` de los CLIs del experimento de codex): inyecta
    el cliente explícito sin tocar la config persistida del consumer. Codex sin sesión/binario →
    `CodexError` propaga (es un override explícito; no hay a quién caer).
    """
    return _build_one(
        provider,
        LLMConsumerSettings(provider=provider, model=model, codex_model=codex_model),
    )


async def aclose_llm(client: LLMClient) -> None:
    """Cierra un cliente del Protocol si expone `aclose` (Codex no lo tiene → no-op)."""
    closer = getattr(client, "aclose", None)
    if closer is not None:
        await closer()
