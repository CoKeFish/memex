"""CLIs de los módulos de extracción.

- `memex-extract` (main): run / enable / modules
    run      — una pasada de extracción sobre los mensajes clasificados no-extraídos (usa LLM)
    enable   — habilita un módulo para un user (upsert en module_settings; sin LLM)
    modules  — lista los módulos registrados y su estado enabled (introspección; sin LLM)
- `memex-process` (main_process): run — corrida combinada resumen + extracción

Server-side + async. `run`/`process` necesitan DEEPSEEK_API_KEY (inyectada por `doppler run`).
Exit 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv
from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMError
from memex.logging import get_logger, setup_logging
from memex.modules import known_modules
from memex.modules.orchestrator import run_extraction
from memex.modules.process import run_combined

_LLM_ERR_MSG = "\nERROR LLM. ¿Corriste con `doppler run -- ...` (DEEPSEEK_API_KEY)?\n"

# --- memex-extract ----------------------------------------------------------------- #


def _build_extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-extract")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Extrae datos de los mensajes clasificados no-extraídos.")
    run_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    run_p.add_argument("--source", type=int, default=None, help="Limitar a este source id.")
    run_p.add_argument("--limit", type=int, default=200, help="Máximo de mensajes (default 200).")

    en_p = sub.add_parser("enable", help="Habilita un módulo para un user (upsert).")
    en_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    en_p.add_argument("--module", required=True, help="Slug del módulo (ej. finance).")

    mod_p = sub.add_parser("modules", help="Lista módulos registrados y su estado.")
    mod_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    return parser


def _cmd_extract_run(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_extraction(args.user, source_id=args.source, limit=args.limit))
    mods = ", ".join(f"{slug}={n}" for slug, n in sorted(stats.by_module.items())) or "—"
    print(
        f"\nextract: ventanas={stats.windows} items={stats.items} "
        f"descartados={stats.discarded} errores={stats.errors} | {mods}\n"
    )
    return 0


def _cmd_enable(args: argparse.Namespace) -> int:
    if args.module not in known_modules():
        print(
            f"\nmódulo desconocido: {args.module!r}. Conocidos: {known_modules()}\n",
            file=sys.stderr,
        )
        return 1
    with connection() as conn:
        conn.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, enabled) "
                "VALUES (:u, :slug, TRUE) "
                "ON CONFLICT (user_id, module_slug) DO UPDATE SET enabled = TRUE"
            ),
            {"u": args.user, "slug": args.module},
        )
    print(f"\nmódulo '{args.module}' habilitado para user {args.user}.\n")
    return 0


def _cmd_modules(args: argparse.Namespace) -> int:
    with connection() as conn:
        enabled = set(
            conn.execute(
                text("SELECT module_slug FROM module_settings WHERE user_id = :u AND enabled"),
                {"u": args.user},
            )
            .scalars()
            .all()
        )
    print(f"\nmódulos (user {args.user}):")
    for slug in known_modules():
        print(f"  {slug}: {'enabled' if slug in enabled else 'disabled'}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.cli")

    args = _build_extract_parser().parse_args(argv)
    log.info("extract.cli.start", cmd=args.cmd)
    try:
        if args.cmd == "run":
            return _cmd_extract_run(args)
        if args.cmd == "enable":
            return _cmd_enable(args)
        if args.cmd == "modules":
            return _cmd_modules(args)
        log.error("extract.cli.unknown_command", cmd=args.cmd)
        return 1
    except LLMError as e:
        log.error("extract.cli.llm_error", status_code=e.status_code, msg=str(e))
        print(_LLM_ERR_MSG, file=sys.stderr)
        return 1
    except Exception as e:
        log.exception("extract.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


# --- memex-process ----------------------------------------------------------------- #


def _build_process_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-process")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run", help="Corrida combinada: resumen + extracción.")
    run_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    run_p.add_argument("--source", type=int, default=None, help="Limitar a este source id.")
    run_p.add_argument("--limit", type=int, default=200, help="Máximo de mensajes (default 200).")
    return parser


def _cmd_process_run(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_combined(args.user, source_id=args.source, limit=args.limit))
    print(
        f"\nprocess: resúmenes={stats.summarize.summaries} "
        f"items_extraídos={stats.extract.items} (descartados={stats.extract.discarded})\n"
    )
    return 0


def main_process(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.cli")

    args = _build_process_parser().parse_args(argv)
    log.info("process.cli.start", cmd=args.cmd)
    try:
        if args.cmd == "run":
            return _cmd_process_run(args)
        log.error("process.cli.unknown_command", cmd=args.cmd)
        return 1
    except LLMError as e:
        log.error("process.cli.llm_error", status_code=e.status_code, msg=str(e))
        print(_LLM_ERR_MSG, file=sys.stderr)
        return 1
    except Exception as e:
        log.exception("process.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
