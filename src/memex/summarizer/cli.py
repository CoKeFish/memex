"""CLI `memex-summarize` — resume el inbox clasificado por tier (usa el LLM).

Subcomando:
  run  — una pasada de resumen sobre los mensajes clasificados no-resumidos de un user
         (filtrable por --source y --tier, acotable por --limit).

Server-side + async. Necesita DEEPSEEK_API_KEY (inyectada por `doppler run`).
Exit code 0 si OK; 1 si error fatal (config LLM faltante, etc.).
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from memex.llm import LLMError
from memex.logging import get_logger, setup_logging
from memex.summarizer.worker import run_summarization


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-summarize")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Resume el inbox clasificado no-resumido de un user.")
    run_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    run_p.add_argument("--source", type=int, default=None, help="Limitar a este source id.")
    run_p.add_argument(
        "--tier", choices=["batch", "individual"], default=None, help="Limitar a un tier."
    )
    run_p.add_argument("--limit", type=int, default=200, help="Máximo de mensajes (default 200).")
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_summarization(args.user, source_id=args.source, tier=args.tier, limit=args.limit)
    )
    tiers = ", ".join(f"{tier}={count}" for tier, count in sorted(stats.by_tier.items())) or "—"
    print(
        f"\nsummarize: resúmenes={stats.summaries} mensajes cubiertos={stats.messages} | {tiers}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.summarizer.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    log.info("summarizer.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "run":
            return _cmd_run(args)
        log.error("summarizer.cli.unknown_command", cmd=args.cmd)
        return 1
    except LLMError as e:
        log.error("summarizer.cli.llm_error", status_code=e.status_code, msg=str(e))
        print(
            "\nERROR LLM. ¿Corriste con `doppler run -- ...` (DEEPSEEK_API_KEY)?\n",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        log.exception("summarizer.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
