"""CLI `memex-finance` — workers post-extracción de finance.

Subcomandos:
  register     — registra una transacción determinista (entrada por agente, sin LLM).
  show         — detalle de un pago consolidado, con el lugar resuelto del catálogo.
  set-place    — asocia un lugar del catálogo (`geo_places`) a un pago consolidado.
  dedup        — FASE 2: resuelve con LLM los pares candidatos (confirmar/rechazar).
  consolidate  — reconstruye la proyección consolidada (grupos + ganador por completitud).
  geo          — resuelve el lugar GPS (dónde estuviste) de transacciones con hora precisa.
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
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.geo import GeoConfigError, GeoError, build_provider_from_env
from memex.geo import places as place_catalog
from memex.logging import get_logger, setup_logging
from memex.modules.finance.consolidate import run_consolidation
from memex.modules.finance.dedup_llm import run_dedup_phase2
from memex.modules.finance.geo_places import resolve_transaction_places
from memex.modules.finance.manual import set_place, show_transaction
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
  show         detalle de un pago consolidado (incluye el lugar resuelto del catálogo)
  set-place    asocia un lugar del catálogo a un pago consolidado ("este pago fue en X")
  dedup        FASE 2 de dedup con LLM (mantenimiento)
  consolidate  reconstruye la proyección consolidada (reconciliador/mantenimiento)
  geo          resuelve el lugar GPS de transacciones con hora precisa (on-demand, sin LLM)
  help         muestra esta ayuda

register (entrada del agente):
  --amount <n> --currency <ISO4217> [--direction ingreso|egreso] [--category]
  [--counterparty "<comercio>"] [--place] [--occurred-at ISO] [--event <id>] [--json]

show / set-place (sobre el pago CONSOLIDADO; register --json devuelve consolidated_id):
  show      --id <n> [--json]
  set-place --id <n> (--place-id <catálogo> | --text "<lugar>" | --clear) [--json]
            --text geocodifica si el texto no está cacheado (gasta una llamada a Maps);
            el counterparty NUNCA se geocodifica solo.

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

    geo_p = sub.add_parser(
        "geo",
        help="Resuelve el lugar GPS de transacciones con hora precisa (on-demand, sin LLM).",
    )
    geo_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    geo_p.add_argument("--limit", type=int, default=100, help="Máx transacciones (default 100).")
    geo_p.add_argument(
        "--no-poi", action="store_true", help="Solo dirección (no buscar el nombre del negocio)."
    )
    geo_p.add_argument(
        "--max-staleness-min",
        type=int,
        default=15,
        help="Tolerancia en minutos entre el cobro y el ping más cercano (default 15).",
    )

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

    show_p = sub.add_parser(
        "show", help="Detalle de un pago consolidado (incluye el lugar resuelto del catálogo)."
    )
    show_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    show_p.add_argument(
        "--id",
        type=int,
        required=True,
        help="Id del pago consolidado (register --json lo devuelve como consolidated_id).",
    )
    show_p.add_argument("--json", action="store_true", dest="as_json")

    place_p = sub.add_parser(
        "set-place",
        help="Asocia un lugar del catálogo (geo_places) a un pago consolidado.",
    )
    place_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    place_p.add_argument(
        "--id",
        type=int,
        required=True,
        help="Id del pago consolidado (register --json lo devuelve como consolidated_id).",
    )
    how = place_p.add_mutually_exclusive_group(required=True)
    how.add_argument(
        "--place-id",
        type=int,
        default=None,
        help="Lugar por id del catálogo (listalos con 'memex-geo places').",
    )
    how.add_argument(
        "--text",
        default=None,
        help="Lugar por texto: se resuelve contra el catálogo (geocodifica si hace falta).",
    )
    how.add_argument("--clear", action="store_true", help="Quita la asociación de lugar del pago.")
    place_p.add_argument("--json", action="store_true", dest="as_json")

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


def _cmd_geo(args: argparse.Namespace) -> int:
    try:
        stats = asyncio.run(
            resolve_transaction_places(
                args.user,
                limit=args.limit,
                want_poi=not args.no_poi,
                max_staleness=timedelta(minutes=args.max_staleness_min),
            )
        )
    except GeoConfigError as e:  # subclase de GeoError → atrapar primero
        _say(
            f"Config geo inválida: {e}. ¿Corriste con `doppler run -- memex-finance geo`? "
            "¿Está seteada GMAPS_API_KEY?",
            err=True,
        )
        return 1
    except GeoError as e:
        _say(f"Error del proveedor de mapas: {e}", err=True)
        return 1
    aborted = " (cortado por cuota/permiso)" if stats.aborted else ""
    _say(
        f"\nfinance geo: vistas={stats.scanned} resueltas={stats.resolved} "
        f"en_transito={stats.in_transit} sin_fix={stats.no_fix} "
        f"sin_resultado={stats.no_result}{aborted}\n"
    )
    return 0


def register_from_args(
    conn: Connection,
    user_id: int,
    args: argparse.Namespace,
    *,
    event_id: str | None = None,
    counterparty_identity_id: int | None = None,
) -> dict[str, Any]:
    """Mapea `args` (ya parseados) → `finance.register` sobre un `conn` DADO. Lo reusan
    `_cmd_register` (tx propia) y el cierre de evento del agente (tx compartida, con la identidad
    del evento). `event_id` pisa `args.event` si viene."""
    occurred_at, precision = _parse_when(args.occurred_at)
    return register(
        conn,
        user_id,
        amount=Decimal(args.amount),
        currency=args.currency,
        direction=args.direction,
        category=args.category,
        counterparty=args.counterparty,
        place=args.place,
        occurred_at=occurred_at,
        occurred_at_precision=precision,
        description=args.description,
        event_id=event_id if event_id is not None else args.event,
        counterparty_identity_id=counterparty_identity_id,
    )


def _cmd_register(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = register_from_args(conn, args.user, args)
    if args.as_json:
        _emit_json(row)
    else:
        cp = f" · {row['counterparty']}" if row["counterparty"] else ""
        _say(
            f"registrada #{row['id']}: {row['direction']} {row['amount']} {row['currency']}{cp} "
            f"(consolidado #{row['consolidated_id']})"
        )
    return 0


def _print_detail(detail: dict[str, Any]) -> None:
    """Bloque humano del pago consolidado (estilo `memex calendario show`)."""
    _say(
        f"\npago #{detail['id']}: {detail['direction']} {detail['amount']} "
        f"{detail['currency']} · {detail['category']}"
    )
    if detail["counterparty"]:
        _say(f"  contraparte:    {detail['counterparty']}")
    if detail["place"]:
        _say(f"  lugar (texto):  {detail['place']}")
    if detail["place_id"] is not None:
        addr = f" — {detail['place_address']}" if detail["place_address"] else ""
        _say(f"  lugar resuelto: {detail['place_name']}{addr} (lugar #{detail['place_id']})")
    _say(f"  cuándo:         {detail['occurred_at']} ({detail['occurred_at_precision']})")
    if detail["description"]:
        _say(f"  descripción:    {detail['description']}")
    _say("")


def _cmd_show(args: argparse.Namespace) -> int:
    with connection() as conn:
        detail = show_transaction(conn, args.user, args.id)
    if detail is None:
        _say(f"el pago consolidado #{args.id} no existe", err=True)
        return 1
    if args.as_json:
        _emit_json(detail)
    else:
        _print_detail(detail)
    return 0


async def _set_place_by_text(
    user_id: int, consolidated_id: int, query_text: str
) -> dict[str, Any] | None:
    """Valida el pago, resuelve el texto contra el catálogo (caché primero; geocodifica el miss) y
    asocia. None = ZERO_RESULTS (sin lugar para ese texto). La validación va ANTES de geocodificar
    para no gastar la llamada a Maps si el pago no existe."""
    provider = build_provider_from_env()
    try:
        with connection() as conn:
            if show_transaction(conn, user_id, consolidated_id) is None:
                raise ValueError(f"el pago consolidado #{consolidated_id} no existe")
            place_id = await place_catalog.resolve_place(conn, user_id, query_text, provider)
            if place_id is None:
                return None
            return set_place(conn, user_id, consolidated_id, place_id)
    finally:
        await provider.aclose()


def _cmd_set_place(args: argparse.Namespace) -> int:
    try:
        if args.text is not None:
            try:
                detail = asyncio.run(_set_place_by_text(args.user, args.id, args.text))
            except GeoConfigError as e:  # subclase de GeoError → atrapar primero
                _say(
                    f"Config geo inválida: {e}. ¿Corriste con `doppler run -- memex-finance "
                    "set-place`? ¿Está seteada GMAPS_API_KEY?",
                    err=True,
                )
                return 1
            except GeoError as e:
                _say(f"Error del proveedor de mapas: {e}", err=True)
                return 1
            if detail is None:
                _say(f"sin resultados para {args.text!r}: no se asoció lugar.", err=True)
                return 1
        else:
            place_id = None if args.clear else args.place_id
            with connection() as conn:
                detail = set_place(conn, args.user, args.id, place_id)
    except ValueError as e:
        _say(str(e), err=True)
        return 1
    if args.as_json:
        _emit_json(detail)
    else:
        _print_detail(detail)
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
        if args.cmd == "geo":
            return _cmd_geo(args)
        if args.cmd == "register":
            return _cmd_register(args)
        if args.cmd == "show":
            return _cmd_show(args)
        if args.cmd == "set-place":
            return _cmd_set_place(args)
        log.error("finance.cli.unknown_command", cmd=args.cmd)
        return 1
    except Exception as e:
        log.exception("finance.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
