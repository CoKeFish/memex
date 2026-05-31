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
from memex.llm import LLMError, LLMQuotaError
from memex.logging import get_logger, setup_logging
from memex.modules import known_modules
from memex.modules.orchestrator import run_extraction
from memex.modules.process import run_combined
from memex.processing.windows import MAX_GAP_SECONDS, MAX_WINDOW_SIZE

_LLM_ERR_MSG = "\nERROR LLM. ¿Corriste con `doppler run -- ...` (DEEPSEEK_API_KEY)?\n"
_QUOTA_ERR_MSG = "\nSALDO AGOTADO (HTTP 402): corrida abortada. Recargá saldo del proveedor LLM.\n"


def _positive_int(value: str) -> int:
    """argparse `type` para enteros >= 1 (exit-2 estándar si no)."""
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("debe ser un entero >= 1")
    return parsed


def _nonneg_int(value: str) -> int:
    """argparse `type` para enteros >= 0 (0 = deshabilitado; exit-2 estándar si no)."""
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("debe ser un entero >= 0")
    return parsed


def _positive_float(value: str) -> float:
    """argparse `type` para floats > 0 (exit-2 estándar si no)."""
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("debe ser un número > 0")
    return parsed


def _add_run_tuning_args(parser: argparse.ArgumentParser) -> None:
    """Perillas de ventaneo + ruteo/batching comunes a los `run` de extract/process (ADR-015 §2)."""
    parser.add_argument(
        "--max-window-size",
        type=_positive_int,
        default=MAX_WINDOW_SIZE,
        help=f"Tope de mensajes por ventana batch (default {MAX_WINDOW_SIZE}).",
    )
    parser.add_argument(
        "--max-gap-hours",
        type=_positive_float,
        default=MAX_GAP_SECONDS / 3600,
        help=f"Gap horario que parte una ventana batch (default {MAX_GAP_SECONDS / 3600:g}).",
    )
    parser.add_argument(
        "--route-chunk-size",
        type=_nonneg_int,
        default=0,
        help="Módulos por sub-pasada de ruteo (0 = sin split, default).",
    )
    parser.add_argument(
        "--batching-policy",
        choices=["per_module", "grouped", "all"],
        default="per_module",
        help="Cómo agrupar módulos por llamada de extracción (default per_module).",
    )
    parser.add_argument(
        "--group-size",
        type=_positive_int,
        default=3,
        help="Módulos por llamada con --batching-policy grouped (default 3).",
    )


# --- memex-extract ----------------------------------------------------------------- #


def _build_extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-extract")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_p = sub.add_parser("run", help="Extrae datos de los mensajes clasificados no-extraídos.")
    run_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    run_p.add_argument("--source", type=int, default=None, help="Limitar a este source id.")
    run_p.add_argument("--limit", type=int, default=200, help="Máximo de mensajes (default 200).")
    _add_run_tuning_args(run_p)

    en_p = sub.add_parser("enable", help="Habilita un módulo para un user (upsert).")
    en_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    en_p.add_argument("--module", required=True, help="Slug del módulo (ej. finance).")

    mod_p = sub.add_parser("modules", help="Lista módulos registrados y su estado.")
    mod_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    return parser


def _cmd_extract_run(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_extraction(
            args.user,
            source_id=args.source,
            limit=args.limit,
            max_window_size=args.max_window_size,
            max_gap_seconds=round(args.max_gap_hours * 3600),
            route_chunk_size=args.route_chunk_size,
            batching_policy=args.batching_policy,
            group_size=args.group_size,
        )
    )
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
    except LLMQuotaError as e:
        log.error("extract.cli.quota_abort", status_code=e.status_code, msg=str(e))
        print(_QUOTA_ERR_MSG, file=sys.stderr)
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
    _add_run_tuning_args(run_p)
    return parser


def _cmd_process_run(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_combined(
            args.user,
            source_id=args.source,
            limit=args.limit,
            max_window_size=args.max_window_size,
            max_gap_seconds=round(args.max_gap_hours * 3600),
            route_chunk_size=args.route_chunk_size,
            batching_policy=args.batching_policy,
            group_size=args.group_size,
        )
    )
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
    except LLMQuotaError as e:
        log.error("process.cli.quota_abort", status_code=e.status_code, msg=str(e))
        print(_QUOTA_ERR_MSG, file=sys.stderr)
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
