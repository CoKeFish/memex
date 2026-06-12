"""Construcción del cliente LLM del gate según `settings.provider`.

Un solo lugar decide qué cliente usan el gate y la minería cuando el caller no inyecta uno:
- `anthropic` (default): API por token (`ANTHROPIC_API_KEY`), métricas completas en llm_calls.
- `codex`: `codex exec` con la suscripción del dueño — SOLO host-side (binario + `codex login`
  en la máquina; dentro del contenedor falla con `CodexError` accionable) y sin métricas de
  tokens (costo $0 en llm_calls). `codex_model` None = el default del CLI.
"""

from __future__ import annotations

from memex.llm import AnthropicClient, CodexClient, LLMClient, anthropic_config
from memex.relevance.settings import GateSettings


def build_gate_client(settings: GateSettings) -> LLMClient:
    """Cliente default del gate/minería para estos settings (el caller es dueño de cerrarlo)."""
    if settings.provider == "codex":
        return CodexClient(model=settings.codex_model)
    return AnthropicClient(anthropic_config())
