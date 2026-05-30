"""CLI `memex-classify` — clasifica el inbox por tier (ADR-002), sin LLM.

Subcomando:
  run  — una pasada de clasificación sobre el inbox no-clasificado de un user
         (filtrable por --source, acotable por --limit, --dry-run para preview).

Worker server-side: habla con la DB directo (memex.db), no es un ingestor.
Exit code 0 si OK; 1 si hubo un error fatal.
"""

from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from memex.classifier.worker import run_classification
from memex.logging import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-classify")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Clasifica el inbox no-clasificado de un user.")
    run_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    run_p.add_argument(
        "--source", type=int, default=None, help="Limitar a este source id (default: todos)."
    )
    run_p.add_argument("--limit", type=int, default=500, help="Máximo de mensajes (default 500).")
    run_p.add_argument(
        "--dry-run", action="store_true", help="Calcula tiers sin escribir en classifications."
    )
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    stats = run_classification(
        args.user, source_id=args.source, limit=args.limit, dry_run=args.dry_run
    )
    tiers = ", ".join(f"{tier}={count}" for tier, count in sorted(stats.by_tier.items())) or "—"
    mode = " (dry-run)" if args.dry_run else ""
    print(
        f"\nclassify{mode}: escaneados={stats.scanned} clasificados={stats.classified} | {tiers}\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.classifier.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    log.info("classifier.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "run":
            return _cmd_run(args)
        log.error("classifier.cli.unknown_command", cmd=args.cmd)
        return 1
    except Exception as e:
        log.exception("classifier.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
