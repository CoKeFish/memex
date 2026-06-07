"""CLI `memex-finance` — workers post-extracción de finance.

Subcomandos:
  register     — registra una transacción determinista (entrada por agente, sin LLM).
  dedup        — FASE 2: resuelve con LLM los pares candidatos (confirmar/rechazar).
  consolidate  — reconstruye la proyección consolidada (grupos + ganador por completitud).
  help         — resumen de los comandos (para que el agente descubra la CLI).

Server-side (corre DENTRO de memex): habla con la DB vía `connection()`, igual que
`memex-calendar-sync`. `dedup` necesita las env vars del LLM (inyectadas por `doppler run`);
`consolidate` es determinista (solo DB). El orden natural en el procesamiento manual es: correr la
extracción (que ya resuelve identidad), después `dedup`, después `consolidate`.

Exit code 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime, time
from decimal import Decimal

from dotenv import load_dotenv

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.finance.consolidate import run_consolidation
from memex.modules.finance.dedup_llm import run_dedup_phase2
from memex.modules.finance.module import register


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows): evita que
    `print()` reviente con acentos/guiones."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: object) -> None:
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


def _parse_when(s: str | None) -> tuple[datetime | None, str | None]:
    """ISO 8601: solo fecha → 'date' (medianoche UTC); con hora → 'datetime' (naive=UTC)."""
    if not s:
        return None, None
    s = s.strip()
    try:
        d = date.fromisoformat(s)
        return datetime.combine(d, time.min, tzinfo=UTC), "date"
    except ValueError:
        pass
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt, "datetime"


_HELP = """memex-finance — finanzas: transacciones + dedup + consolidación.

Comandos:
  register     registra una transacción; asegura su consolidado y teje aristas en el acto
  dedup        FASE 2 de dedup con LLM (mantenimiento)
  consolidate  reconstruye la proyección consolidada (reconciliador/mantenimiento)
  help         muestra esta ayuda

register (entrada del agente):
  --amount <n> --currency <ISO4217> [--direction ingreso|egreso] [--category]
  [--counterparty "<comercio>"] [--place] [--occurred-at ISO] [--event <id>] [--json]

Reglas:
  --event <id>  hechos del MISMO mensaje comparten el id (factura = gasto + comida)
  --json        la respuesta JSON es la ÚLTIMA línea de stdout

Flags de cada comando: memex-finance <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-finance")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("help", help="Resumen de los comandos (para descubrir la CLI).")

    dedup_p = sub.add_parser(
        "dedup", help="Dedup FASE 2: resuelve con LLM los pares candidatos (confirmar/rechazar)."
    )
    dedup_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    dedup_p.add_argument("--limit", type=int, default=200, help="Máximo de pares (default 200).")

    cons_p = sub.add_parser(
        "consolidate", help="Reconstruye la vista consolidada (grupos + ganador por completitud)."
    )
    cons_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    reg_p = sub.add_parser(
        "register", help="Registra una transacción determinista (entrada por agente, sin LLM)."
    )
    reg_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    reg_p.add_argument("--amount", required=True, help="Monto POSITIVO (sin separadores de miles).")
    reg_p.add_argument("--currency", required=True, help="ISO 4217 (USD, COP, …).")
    reg_p.add_argument("--direction", default="egreso", choices=("ingreso", "egreso"))
    reg_p.add_argument("--category", default="otros")
    reg_p.add_argument(
        "--counterparty", default="", help="Comercio/persona (se resuelve a identidad)."
    )
    reg_p.add_argument("--place", default="", help="Lugar físico o URL.")
    reg_p.add_argument(
        "--occurred-at", default=None, help="ISO 8601 (fecha o fecha-hora). Sin esto, ahora."
    )
    reg_p.add_argument("--description", default="")
    reg_p.add_argument(
        "--event", default=None, help="Id de correlación (hechos del mismo mensaje)."
    )
    reg_p.add_argument("--json", action="store_true", dest="as_json")

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


def _cmd_register(args: argparse.Namespace) -> int:
    occurred_at, precision = _parse_when(args.occurred_at)
    with connection() as conn:
        row = register(
            conn,
            args.user,
            amount=Decimal(args.amount),
            currency=args.currency,
            direction=args.direction,
            category=args.category,
            counterparty=args.counterparty,
            place=args.place,
            occurred_at=occurred_at,
            occurred_at_precision=precision,
            description=args.description,
            event_id=args.event,
        )
    if args.as_json:
        _emit_json(row)
    else:
        cp = f" · {row['counterparty']}" if row["counterparty"] else ""
        _say(f"registrada #{row['id']}: {row['direction']} {row['amount']} {row['currency']}{cp}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.finance.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "help":
        _say(_HELP)
        return 0
    log.info("finance.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "dedup":
            return _cmd_dedup(args)
        if args.cmd == "consolidate":
            return _cmd_consolidate(args)
        if args.cmd == "register":
            return _cmd_register(args)
        log.error("finance.cli.unknown_command", cmd=args.cmd)
        return 1
    except Exception as e:
        log.exception("finance.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
