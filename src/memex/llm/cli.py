"""CLI `memex-llm` — selección de proveedor+modelo LLM por consumidor (`llm_consumer_settings`).

Subcomandos:
  consumers          — lista las claves de consumer válidas + los proveedores.
  settings show      — filas configuradas del usuario (las ausentes usan el default/hardcode).
  settings set       — fija provider/model/codex-model/fallback de un consumer (upsert parcial).

Conecta directo a Postgres. La fábrica `memex.llm.registry.build_llm_client` lee estas filas en
runtime; sin fila para un consumer cae a la fila `default`, y sin esa, al hardcode DeepSeek
(preserva el comportamiento previo). NO dispara LLM — solo configura.

Ejemplos:
  memex-llm consumers
  memex-llm settings set --consumer summarizer --provider codex --codex-model gpt-5.1
  memex-llm settings set --consumer orchestrator --provider deepseek --fallback anthropic
  memex-llm settings set --consumer quality_judge --model ''   # limpia el override de modelo
  memex-llm settings show
"""

from __future__ import annotations

import argparse
import sys

from memex.db import connection
from memex.llm.settings import (
    LLM_CONSUMERS,
    LLM_PROVIDERS,
    LLMConsumerSettings,
    list_consumer_settings,
    upsert_consumer_settings,
)
from memex.logging import setup_logging


def _fmt(consumer: str, s: LLMConsumerSettings) -> str:
    parts = [f"{consumer}: provider={s.provider} model={s.model or '(default)'}"]
    if s.provider == "codex" or "codex" in s.fallback:
        parts.append(f"codex_model={s.codex_model or 'default'}")
    if s.fallback:
        parts.append(f"fallback={','.join(s.fallback)}")
    return " ".join(parts)


def cmd_consumers(args: argparse.Namespace) -> int:
    print("consumers:", ", ".join(LLM_CONSUMERS))
    print("providers:", ", ".join(LLM_PROVIDERS))
    return 0


def cmd_settings_show(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = list_consumer_settings(conn, args.user_id)
    if not rows:
        print("(sin filas configuradas — todos los consumers usan el default DeepSeek)")
        return 0
    for consumer, s in rows.items():
        print(_fmt(consumer, s))
    return 0


def cmd_settings_set(args: argparse.Namespace) -> int:
    fallback = None
    if args.fallback is not None:
        fallback = [p.strip() for p in args.fallback.split(",") if p.strip()]
    with connection() as conn:
        try:
            s = upsert_consumer_settings(
                conn,
                args.user_id,
                args.consumer,
                provider=args.provider,
                model=args.model,
                codex_model=args.codex_model,
                fallback=fallback,
            )
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    print(_fmt(args.consumer, s))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-llm")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_cons = sub.add_parser("consumers", help="lista consumers y proveedores válidos")
    p_cons.set_defaults(func=cmd_consumers)

    p_set = sub.add_parser("settings", help="config de proveedor por consumidor")
    set_sub = p_set.add_subparsers(dest="action", required=True)

    p_show = set_sub.add_parser("show")
    p_show.add_argument("--user-id", type=int, default=1)
    p_show.set_defaults(func=cmd_settings_show)

    p_setset = set_sub.add_parser("set", help="upsert parcial de un consumer")
    p_setset.add_argument("--user-id", type=int, default=1)
    p_setset.add_argument("--consumer", required=True, choices=list(LLM_CONSUMERS))
    p_setset.add_argument("--provider", default=None, choices=list(LLM_PROVIDERS))
    p_setset.add_argument(
        "--model", default=None, help="modelo del proveedor primario ('' = su default)"
    )
    p_setset.add_argument(
        "--codex-model", default=None, help="modelo de codex ('' = el default del CLI)"
    )
    p_setset.add_argument(
        "--fallback",
        default=None,
        help="cadena de proveedores extra separada por comas ('' = sin fallback)",
    )
    p_setset.set_defaults(func=cmd_settings_set)

    return p


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
