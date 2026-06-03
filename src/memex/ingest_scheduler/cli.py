"""CLI del daemon de ingesta: `memex-ingest-scheduler <subcomando>`.

- `daemon start`           — arranca el loop (bloquea). Apagado por default: sin
                             `ingest_scheduler_settings.daemon_enabled` no trae nada.
- `run <source_id> [--user]` — corre UN fetch incremental de una fuente y sale (sin loop), con
                             `trigger='daemon'`. Herramienta de verificación del camino del daemon:
                             el dueño dispara y mira la fila resultante en `/ingest/runs` + `/logs`.
                             Ignora el master toggle y el `fetch_schedule`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text

from memex.api.fetch_runner import run_fetch_window
from memex.db import connection
from memex.ingest_scheduler.config import IngestSchedulerSettings
from memex.ingest_scheduler.daemon import IngestScheduler
from memex.logging import get_logger, setup_logging


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-ingest-scheduler")
    sub = p.add_subparsers(dest="group", required=True)

    daemon = sub.add_parser("daemon", help="Lifecycle del daemon.")
    dsub = daemon.add_subparsers(dest="cmd", required=True)
    dsub.add_parser("start", help="Arranca el daemon de ingesta (bloquea). Apagado por default.")

    run_p = sub.add_parser("run", help="Corre UN fetch incremental de una fuente y sale.")
    run_p.add_argument("source_id", type=int, help="Fuente a traer una vez.")
    run_p.add_argument("--user", type=int, default=None, help="User id (default: env USER_ID o 1).")

    return p


def _cmd_daemon_start(log: Any) -> int:
    settings = IngestSchedulerSettings()
    log.info("memex_ingest_scheduler.daemon.starting", user_id=settings.user_id)
    # Arranca con lista vacía; el primer tick la puebla desde la DB (`_reload_sources_if_needed`).
    sched = IngestScheduler(
        user_id=settings.user_id, sources=[], tick_seconds=settings.tick_seconds
    )

    async def _serve() -> None:
        sched.install_signal_handlers()
        await sched.run_forever()

    asyncio.run(_serve())
    return 0


def _cmd_run(args: argparse.Namespace, log: Any) -> int:
    settings = IngestSchedulerSettings()
    user_id = args.user if args.user is not None else settings.user_id
    source_id = int(args.source_id)
    with connection() as conn:
        row = (
            conn.execute(
                text(
                    "SELECT type, config, account_id FROM sources "
                    "WHERE id = :sid AND user_id = :uid"
                ),
                {"sid": source_id, "uid": user_id},
            )
            .mappings()
            .first()
        )
    if row is None:
        log.error(
            "memex_ingest_scheduler.run.source_not_found", source_id=source_id, user_id=user_id
        )
        return 1

    async def _once() -> Any:
        return await run_fetch_window(
            user_id=user_id,
            source_id=source_id,
            source_type=str(row["type"]),
            cfg=dict(row["config"] or {}),
            account_id=row["account_id"],
            mode="incremental",
            dry_run=False,
            trigger="daemon",
        )

    log.info("memex_ingest_scheduler.run.start", source_id=source_id, user_id=user_id)
    try:
        stats = asyncio.run(_once())
    except Exception as e:
        log.exception("memex_ingest_scheduler.run.failed", source_id=source_id, exc=str(e))
        return 1
    log.info(
        "memex_ingest_scheduler.run.end",
        source_id=source_id,
        posted=stats.posted,
        inserted=stats.inserted,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.ingest_scheduler.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.group == "daemon" and args.cmd == "start":
            return _cmd_daemon_start(log)
        if args.group == "run":
            return _cmd_run(args, log)
    except Exception as e:
        log.exception("memex_ingest_scheduler.cli.fatal", exc=str(e))
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
