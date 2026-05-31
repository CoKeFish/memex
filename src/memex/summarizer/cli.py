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

from memex.core.deadletter import STAGE_SUMMARIZE, list_review, requeue
from memex.llm import LLMError, LLMQuotaError
from memex.logging import get_logger, setup_logging
from memex.processing.windows import MAX_GAP_SECONDS, MAX_WINDOW_SIZE
from memex.summarizer.worker import run_summarization


def _positive_int(value: str) -> int:
    """argparse `type` para enteros >= 1 (exit-2 estándar si no)."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("debe ser un entero >= 1")
    return parsed


def _positive_float(value: str) -> float:
    """argparse `type` para floats > 0 (exit-2 estándar si no)."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("debe ser un número > 0")
    return parsed


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
    run_p.add_argument(
        "--max-window-size",
        type=_positive_int,
        default=MAX_WINDOW_SIZE,
        help=f"Tope de mensajes por ventana batch (default {MAX_WINDOW_SIZE}).",
    )
    run_p.add_argument(
        "--max-gap-hours",
        type=_positive_float,
        default=MAX_GAP_SECONDS / 3600,
        help=f"Gap horario que parte una ventana batch (default {MAX_GAP_SECONDS / 3600:g}).",
    )

    rev_p = sub.add_parser("review", help="Lista mensajes en revisión (dead-letter).")
    rev_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    rq_p = sub.add_parser("requeue", help="Saca un mensaje de revisión para reintentarlo.")
    rq_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rq_p.add_argument("--inbox", type=int, required=True, help="inbox_id a reencolar.")
    return parser


def _cmd_run(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_summarization(
            args.user,
            source_id=args.source,
            tier=args.tier,
            limit=args.limit,
            max_window_size=args.max_window_size,
            max_gap_seconds=round(args.max_gap_hours * 3600),
        )
    )
    tiers = ", ".join(f"{tier}={count}" for tier, count in sorted(stats.by_tier.items())) or "—"
    print(
        f"\nsummarize: resúmenes={stats.summaries} mensajes cubiertos={stats.messages} | {tiers}\n"
    )
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    items = list_review(args.user, STAGE_SUMMARIZE)
    if not items:
        print("\nsummarize: sin mensajes en revisión.\n")
        return 0
    print(f"\nsummarize — pendientes de revisión (user {args.user}): {len(items)}")
    for it in items:
        err = str(it["last_error"] or "")[:80]
        print(f"  inbox {it['inbox_id']}  intentos={it['attempts']}  {err}")
    print("\nReencolá con: memex-summarize requeue --inbox <id>\n")
    return 0


def _cmd_requeue(args: argparse.Namespace) -> int:
    if requeue(args.user, STAGE_SUMMARIZE, args.inbox):
        print(f"\ninbox {args.inbox} reencolado (vuelve al work-set).\n")
        return 0
    print(f"\ninbox {args.inbox} no estaba en revisión.\n", file=sys.stderr)
    return 1


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
        if args.cmd == "review":
            return _cmd_review(args)
        if args.cmd == "requeue":
            return _cmd_requeue(args)
        log.error("summarizer.cli.unknown_command", cmd=args.cmd)
        return 1
    except LLMQuotaError as e:
        log.error("summarizer.cli.quota_abort", status_code=e.status_code, msg=str(e))
        print(
            "\nSALDO AGOTADO (HTTP 402): corrida abortada. Recargá saldo del proveedor LLM.\n",
            file=sys.stderr,
        )
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
