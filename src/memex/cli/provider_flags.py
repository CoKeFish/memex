"""Flags `--provider/--model/--codex-model` compartidos para overrides de proveedor por corrida.

Los CLIs de los consumidores (summarize, extract/process, jueces) los agregan para correr el
experimento de codex SIN tocar la config persistida: el flag inyecta el cliente de UN proveedor
(override one-off), exactamente como el `--provider` del gate (`memex-relevance`). Sin `--provider`
la corrida usa lo que decida `llm_consumer_settings` para ese consumer (DeepSeek por default).

Vive en `memex.cli` (no en `memex.llm`) porque es plumbing de CLI: imprime el aviso de codex por
stdout, exento de la regla T20 (no-print) que sí aplica al código de aplicación.
"""

from __future__ import annotations

import argparse

from memex.llm.client import LLMClient
from memex.llm.registry import build_provider_client
from memex.llm.settings import LLM_PROVIDERS


def add_provider_flags(parser: argparse.ArgumentParser) -> None:
    """Agrega `--provider/--model/--codex-model` a un subcomando que corre LLM."""
    parser.add_argument(
        "--provider",
        choices=list(LLM_PROVIDERS),
        default=None,
        help="override del proveedor por corrida (default: la config del consumer / DeepSeek)",
    )
    parser.add_argument(
        "--model", default=None, help="modelo del proveedor (override; deepseek/anthropic)"
    )
    parser.add_argument(
        "--codex-model",
        default=None,
        help="modelo para --provider codex (default: el del CLI de codex)",
    )


def client_from_flags(args: argparse.Namespace) -> LLMClient | None:
    """Cliente override según los flags, o None si no se pasó `--provider`.

    codex avisa por stdout (suscripción, sin métricas de costo). El cliente construido es de UN
    solo proveedor (sin cadena de fallback): el override es deliberado.
    """
    provider = getattr(args, "provider", None)
    if provider is None:
        return None
    if provider == "codex":
        print("(proveedor codex: costo no medido en llm_calls, consume tu suscripcion)")
    return build_provider_client(provider, model=args.model, codex_model=args.codex_model)
