"""Construcción del cliente LLM del gate según `settings.provider` (intercambiable por config).

Un solo lugar decide qué cliente usan el gate, la minería y el lazo de intereses cuando el caller
no inyecta uno. Delega en la MISMA fábrica de proveedores que el resto del sistema
(`build_provider_client`), así sumar/cambiar proveedor no duplica lógica:
- `anthropic` (default): API por token (`ANTHROPIC_API_KEY`), métricas completas en llm_calls.
- `codex`: `codex exec` con la suscripción del dueño — SOLO host-side; sin métricas (costo $0).
- `deepseek`: API barata (`DEEPSEEK_API_KEY`), su modelo por env; el fallback natural si codex
  se agota. `settings.complete_model` es None para deepseek/codex (no se les pasa `model`).
"""

from __future__ import annotations

from memex.llm import LLMClient, build_provider_client
from memex.relevance.settings import GateSettings


def build_gate_client(settings: GateSettings) -> LLMClient:
    """Cliente default del gate/minería para estos settings (el caller es dueño de cerrarlo)."""
    return build_provider_client(
        settings.provider,
        model=settings.complete_model,
        codex_model=settings.codex_model,
    )
