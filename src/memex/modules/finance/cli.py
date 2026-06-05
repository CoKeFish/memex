"""CLI `memex-finance` — workers post-extracción de finance.

Subcomandos:
  dedup        — FASE 2: resuelve con LLM los pares candidatos (confirmar/rechazar).
  consolidate  — reconstruye la proyección consolidada (grupos + ganador por completitud).

Server-side (corre DENTRO de memex): habla con la DB vía `connection()`, igual que
`memex-calendar-sync`. `dedup` necesita las env vars del LLM (inyectadas por `doppler run`);
`consolidate` es determinista (solo DB). El orden natural en el procesamiento manual es: correr la
extracción (que ya resuelve identidad), después `dedup`, después `consolidate`.

Exit code 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from memex.logging import get_logger, setup_logging
from memex.modules.finance.consolidate import run_consolidation
from memex.modules.finance.dedup_llm import run_dedup_phase2


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows): evita que
    `print()` reviente con acentos/guiones."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-finance")
    sub = parser.add_subparsers(dest="cmd", required=True)

    dedup_p = sub.add_parser(
        "dedup", help="Dedup FASE 2: resuelve con LLM los pares candidatos (confirmar/rechazar)."
    )
    dedup_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    dedup_p.add_argument("--limit", type=int, default=200, help="Máximo de pares (default 200).")

    cons_p = sub.add_parser(
        "consolidate", help="Reconstruye la vista consolidada (grupos + ganador por completitud)."
    )
    cons_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    return parser


def _cmd_dedup(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_dedup_phase2(args.user, limit=args.limit))
    _say(
        f"\nfinance dedup F2: pares={stats.pairs} confirmados={stats.confirmed} "
        f"rechazados={stats.rejected} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_consolidate(args: argparse.Namespace) -> int:
    stats = run_consolidation(args.user)
    _say(
        f"\nfinance consolidate: grupos={stats.groups} consolidados={stats.consolidated} "
        f"merges={stats.merges}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.finance.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    log.info("finance.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "dedup":
            return _cmd_dedup(args)
        if args.cmd == "consolidate":
            return _cmd_consolidate(args)
        log.error("finance.cli.unknown_command", cmd=args.cmd)
        return 1
    except Exception as e:
        log.exception("finance.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
