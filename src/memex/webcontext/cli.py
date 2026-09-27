"""CLI `memex-webcontext` — perfil web de una entidad (org/producto) desde la terminal.

Subcomando:
  search  — nombre + tipo → perfil estructurado garantizado + fuentes, vía la cadena de proveedores
            (default codex→firecrawl; `--provider` fuerza uno).

Server-side: codex usa la sesión de `CODEX_HOME`; firecrawl usa `FIRECRAWL_API_KEY` (Doppler →
correr con `doppler run -- memex-webcontext ...`). Exit 0 si OK; 1 si error de proveedor/config; 2
si argumentos inválidos.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from dotenv import load_dotenv

from memex.logging import get_logger, setup_logging
from memex.webcontext import (
    ProfileResult,
    WebContextConfigError,
    WebContextError,
    WebContextNotFoundError,
    build_provider_from_env,
    known_providers,
    search_entity,
)

_ENTITY_KINDS = ("organizacion", "producto")


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows)."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-webcontext")
    sub = parser.add_subparsers(dest="cmd", required=True)
    search_p = sub.add_parser("search", help="Perfil web de una entidad (org/producto).")
    search_p.add_argument("--name", required=True, help="Nombre de la entidad.")
    search_p.add_argument(
        "--kind",
        required=True,
        choices=_ENTITY_KINDS,
        help="Tipo de entidad (nunca persona).",
    )
    search_p.add_argument(
        "--provider",
        choices=known_providers(),
        default=None,
        help="Forzar un proveedor (default: cadena MEMEX_WEBCONTEXT_PROVIDER o codex→firecrawl).",
    )
    search_p.add_argument("--json", action="store_true", help="Salida JSON (machine-readable).")
    return parser


def _print_profile(result: ProfileResult, *, as_json: bool) -> None:
    p = result.profile
    if as_json:
        tokens = (
            {"input": result.tokens.input_tokens, "output": result.tokens.output_tokens}
            if result.tokens is not None
            else None
        )
        _say(
            json.dumps(
                {
                    "name": p.name,
                    "kind": p.kind,
                    "one_liner": p.one_liner,
                    "sector": p.sector,
                    "country": p.country,
                    "founded": p.founded,
                    "key_facts": list(p.key_facts),
                    "sources": list(p.sources),
                    "provider": result.provider,
                    "latency_ms": result.latency_ms,
                    "tokens": tokens,
                },
                ensure_ascii=False,
            )
        )
        return
    _say(f"{p.name} ({p.kind}) — vía {result.provider}")
    if p.one_liner:
        _say(f"  {p.one_liner}")
    meta = [x for x in (p.sector, p.country, p.founded) if x]
    if meta:
        _say(f"  {' · '.join(meta)}")
    for fact in p.key_facts:
        _say(f"  - {fact}")
    for src in p.sources:
        _say(f"  → {src}")


async def _cmd_search(args: argparse.Namespace) -> int:
    provider = build_provider_from_env(provider=args.provider)
    try:
        result = await search_entity(provider, args.name, args.kind)
    finally:
        await provider.aclose()
    _print_profile(result, as_json=args.json)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.webcontext.cli")
    args = _build_parser().parse_args(argv)

    try:
        if args.cmd == "search":
            return asyncio.run(_cmd_search(args))
    except WebContextNotFoundError as e:
        _say(f"Sin contexto web para {e.query!r}.", err=True)
        return 1
    except WebContextConfigError as e:
        _say(
            f"Config inválida: {e}. ¿Corriste con `doppler run -- memex-webcontext ...`? "
            "¿Está seteada FIRECRAWL_API_KEY (si usás firecrawl)? ¿La sesión de codex está viva?",
            err=True,
        )
        return 1
    except WebContextError as e:
        _say(f"Error del proveedor de contexto web: {e}", err=True)
        log.warning("webcontext.cli.error", error=str(e))
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
