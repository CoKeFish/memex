"""CLI del scheduler server-side: `memex-scheduler <subcomando>`.

- `daemon start`        — arranca el loop (bloquea). Apagado por default: con `enabled_jobs` vacío
                          idlea sin procesar nada.
- `run <job> [--user]`  — corre UNA pasada de un job y sale (sin loop), escribiendo su fila en
                          `worker_runs`. Herramienta del BACKFILL controlado: el dueño dispara,
                          mira costo en `worker_runs`/`llm_calls`, repite. Ignora `enabled_jobs`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from dotenv import load_dotenv

from memex.logging import get_logger, setup_logging
from memex.scheduler import runs
from memex.scheduler.config import SchedulerSettings, build_jobs
from memex.scheduler.daemon import AsyncScheduler
from memex.scheduler.jobs import all_jobs


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-scheduler")
    sub = p.add_subparsers(dest="group", required=True)

    daemon = sub.add_parser("daemon", help="Lifecycle del daemon.")
    dsub = daemon.add_subparsers(dest="cmd", required=True)
    dsub.add_parser("start", help="Arranca el scheduler (bloquea). Apagado por default.")

    run_p = sub.add_parser("run", help="Corre UNA pasada de un job y sale (backfill controlado).")
    run_p.add_argument("job", choices=sorted(all_jobs()), help="Job a correr una vez.")
    run_p.add_argument(
        "--user", type=int, default=None, help="User id (default: MEMEX_SCHEDULER_USER_ID o 1)."
    )

    return p


def _cmd_daemon_start(log: Any) -> int:
    settings = SchedulerSettings()
    jobs = build_jobs(settings)
    log.info(
        "memex_scheduler.daemon.starting",
        user_id=settings.user_id,
        jobs=[j.name for j in jobs],
    )
    sched = AsyncScheduler(user_id=settings.user_id, jobs=jobs, tick_seconds=settings.tick_seconds)

    async def _serve() -> None:
        sched.install_signal_handlers()
        await sched.run_forever()

    asyncio.run(_serve())
    return 0


def _cmd_run(args: argparse.Namespace, log: Any) -> int:
    settings = SchedulerSettings()
    user_id = args.user if args.user is not None else settings.user_id
    job = all_jobs()[args.job]
    log.info("memex_scheduler.run.start", job=job.name, user_id=user_id)

    async def _once() -> Any:
        return await job.run(user_id)

    run_id = runs.start_run(user_id, job.name)
    try:
        stats = asyncio.run(_once())
    except Exception as e:
        runs.finish_run(run_id, status="error", error=str(e))
        log.exception("memex_scheduler.run.failed", job=job.name, exc=str(e))
        return 1
    runs.finish_run(run_id, status="ok", stats=stats)
    log.info("memex_scheduler.run.end", job=job.name)
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.scheduler.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.group == "daemon" and args.cmd == "start":
            return _cmd_daemon_start(log)
        if args.group == "run":
            return _cmd_run(args, log)
    except Exception as e:
        log.exception("memex_scheduler.cli.fatal", exc=str(e))
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
