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
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from dotenv import load_dotenv
from sqlalchemy.engine import Connection

from memex.core.localtime import LOCAL_TZ, local_instant
from memex.db import connection
from memex.geo import GeoConfigError, GeoError, build_provider_from_env
from memex.geo import places as place_catalog
from memex.logging import get_logger, setup_logging
from memex.modules.finance import subscriptions as subs_mod
from memex.modules.finance.consolidate import run_consolidation
from memex.modules.finance.dedup import DedupRow
from memex.modules.finance.dedup_llm import run_dedup_phase2
from memex.modules.finance.geo_places import resolve_transaction_places
from memex.modules.finance.manual import (
    list_transactions,
    pending_payments,
    remove_transaction,
    set_place,
    show_transaction,
    summarize_transactions,
)
from memex.modules.finance.module import register
from memex.modules.finance.remark import PairChange, remark_phase1
from memex.modules.finance.schema import FINANCE_CATEGORIES


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
    """ISO 8601: solo fecha → 'date' (medianoche LOCAL); con hora → 'datetime' (naive = hora
    LOCAL del usuario, `memex.core.localtime`; con offset explícito se respeta)."""
    if not s:
        return None, None
    s = s.strip()
    try:
        d = date.fromisoformat(s)
        return local_instant(d), "date"
    except ValueError:
        pass
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt, "datetime"


_HELP = """memex-finance — finanzas: transacciones + dedup + consolidación.

Comandos:
  register     registra una transacción; asegura su consolidado y teje aristas en el acto
  list         pagos consolidados con filtros (--q --category --direction --status --since --until)
  summary      totales por contraparte|rubro|mes|moneda con los mismos filtros (--by)
  pending      pagos fallidos/pendientes sin pago exitoso posterior al mismo servicio (--days)
  subs         suscripciones: qué se renueva, cuándo y cuánto (--all --json)
  subs-detect  relee los cargos y propone/actualiza suscripciones (determinista, sin LLM)
  subs-set     cancela o descarta una suscripción a mano (queda fijada: el detector no la pisa)
  show         detalle de un pago consolidado (incluye el lugar resuelto del catálogo)
  set-place    asocia un lugar del catálogo a un pago consolidado ("este pago fue en X")
  rm           borra un pago consolidado y sus crudas (deshacer un registro). Definitivo.
  dedup        FASE 2 de dedup con LLM (mantenimiento; --pair <id> re-juzga uno ya decidido)
  consolidate  reconstruye la proyección consolidada (reconciliador/mantenimiento)
  remark       re-marca la FASE 1 (sin LLM) sobre lo ya cargado (--dry-run para ver el diff)
  geo          resuelve el lugar GPS de transacciones con hora precisa (on-demand, sin LLM)
  rubro        rubro por contraparte: "este comercio es comida" (--counterparty --category
               [--note] | --list | --remove <contraparte>); se aplica a todo lo suyo
  recategorize relee lo que quedó en «otros» y lo reclasifica (regla > determinista >
               modelo por lotes; --dry-run para ver; mantenimiento)
  help         muestra esta ayuda

suscripciones:
  subs        [--all] [--json]        ordenadas por la próxima renovación
  subs-detect [--json]                agrupa los cargos por proveedor+moneda y deduce la cadencia
  subs-set --id <n> --status activa|cancelada|descartada [--json]
            'descartada' = no era una suscripción; el detector deja de proponerla

register (entrada del agente):
  --amount <n> --currency <ISO4217> [--direction ingreso|egreso] [--category]
  [--counterparty "<comercio>"] [--place] [--occurred-at ISO] [--event <id>] [--json]

show / set-place / rm (sobre el pago CONSOLIDADO; register --json devuelve consolidated_id):
  show      --id <n> [--json]
  set-place --id <n> (--place-id <catálogo> | --text "<lugar>" | --clear) [--json]
            --text geocodifica si el texto no está cacheado (gasta una llamada a Maps);
            el counterparty NUNCA se geocodifica solo.
  rm        --id <n> [--json]   borra el pago y sus transacciones crudas (deshacer)

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
    dedup_p.add_argument(
        "--pair",
        type=int,
        action="append",
        dest="pairs",
        help="Re-juzga este par (id en mod_finance_dedup_candidates) aunque ya esté decidido; "
        "repetible. Luego correr consolidate.",
    )

    cons_p = sub.add_parser(
        "consolidate", help="Reconstruye la vista consolidada (grupos + ganador por completitud)."
    )
    cons_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    remark_p = sub.add_parser(
        "remark",
        help=(
            "Re-marca la FASE 1 (determinista, sin LLM) sobre lo ya cargado con las reglas "
            "actuales: quita pares que la regla ya no produce, agrega los nuevos, respeta al juez."
        ),
    )
    remark_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    remark_p.add_argument(
        "--dry-run", action="store_true", help="Solo muestra qué pares entrarían/saldrían."
    )
    remark_p.add_argument("--json", action="store_true", help="Resumen JSON en la última línea.")

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
    reg_p.add_argument(
        "--status",
        default="completada",
        choices=("completada", "fallida", "pendiente"),
        help="¿Se movió la plata? Solo 'completada' cuenta en totales (default).",
    )
    reg_p.add_argument(
        "--kind",
        default="pago",
        choices=("pago", "reembolso"),
        help="'reembolso' = plata que vuelve de un cargo previo (descuenta del gasto).",
    )
    reg_p.add_argument(
        "--category",
        default="otros",
        choices=FINANCE_CATEGORIES,
        help="Rubro (lista cerrada); antes un valor fuera de la lista caía a 'otros' EN SILENCIO.",
    )
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

    list_p = sub.add_parser(
        "list", help="Pagos consolidados con filtros (los más recientes primero)."
    )
    _add_filter_flags(list_p)
    list_p.add_argument("--limit", type=int, default=50, help="Máximo de filas (default 50).")
    list_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    subs_p = sub.add_parser(
        "subs", help="Suscripciones: qué se renueva, cuándo y cuánto (por próxima renovación)."
    )
    subs_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    subs_p.add_argument(
        "--all",
        action="store_true",
        dest="include_inactive",
        help="Incluye canceladas y descartadas.",
    )
    subs_p.add_argument("--limit", type=int, default=100, help="Máximo de filas (default 100).")
    subs_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    det_p = sub.add_parser(
        "subs-detect",
        help="Relee los cargos y propone/actualiza suscripciones (determinista, sin LLM).",
    )
    det_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    det_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    sset_p = sub.add_parser(
        "subs-set", help="Cambia el estado de una suscripción a mano (queda fijada)."
    )
    sset_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    sset_p.add_argument("--id", type=int, required=True, help="Id de la suscripción.")
    sset_p.add_argument(
        "--status",
        required=True,
        choices=("activa", "cancelada", "descartada"),
        help="'descartada' = no era una suscripción (el detector deja de proponerla).",
    )
    sset_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    pend_p = sub.add_parser(
        "pending",
        help="Pagos fallidos o pendientes sin un pago exitoso posterior a la misma contraparte.",
    )
    pend_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    pend_p.add_argument(
        "--days", type=int, default=30, help="Ventana para considerar regularizado (default 30)."
    )
    pend_p.add_argument("--limit", type=int, default=100, help="Máximo de filas (default 100).")
    pend_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    sum_p = sub.add_parser(
        "summary",
        help="Totales agrupados (contraparte, rubro, mes o moneda) con los mismos filtros.",
    )
    _add_filter_flags(sum_p)
    sum_p.add_argument(
        "--by",
        default="counterparty",
        choices=("counterparty", "category", "month", "currency"),
        help="Clave de agrupación (default counterparty).",
    )
    sum_p.add_argument("--limit", type=int, default=50, help="Máximo de grupos (default 50).")
    sum_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

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

    rm_p = sub.add_parser(
        "rm", help="Borra un pago consolidado y sus transacciones crudas. Definitivo."
    )
    rm_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rm_p.add_argument("--id", type=int, required=True, help="Id del pago consolidado.")
    rm_p.add_argument("--json", action="store_true", dest="as_json")

    rubro_p = sub.add_parser(
        "rubro",
        help="Rubro por contraparte (tu palabra): se aplica a todo lo de ese comercio, pasado y "
        "futuro.",
    )
    rubro_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rubro_p.add_argument("--counterparty", default=None, help="Contraparte tal como aparece.")
    rubro_p.add_argument("--category", default=None, choices=FINANCE_CATEGORIES, help="Rubro.")
    rubro_p.add_argument("--note", default="", help="Por qué (opcional).")
    rubro_p.add_argument("--list", action="store_true", dest="as_list", help="Lista las reglas.")
    rubro_p.add_argument("--remove", default=None, help="Quita la regla de esa contraparte.")
    rubro_p.add_argument("--json", action="store_true", dest="as_json")

    recat_p = sub.add_parser(
        "recategorize",
        help="Relee lo que quedó en «otros» y lo reclasifica (regla > determinista > modelo).",
    )
    recat_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    recat_p.add_argument("--only", default="otros", help="Rubros a releer, coma-separados.")
    recat_p.add_argument("--dry-run", action="store_true", help="Decide sin escribir.")
    recat_p.add_argument("--json", action="store_true", dest="as_json")

    return parser


def _cmd_rubro(args: argparse.Namespace) -> int:
    from memex.modules.finance import rubros

    with connection() as conn:
        if args.remove:
            ok = rubros.remove_rule(conn, args.user, args.remove)
            _say("regla quitada" if ok else "no había regla para esa contraparte")
            return 0
        if args.counterparty and args.category:
            rule = rubros.set_rule(
                conn, args.user, args.counterparty, args.category, note=args.note
            )
            assert rule is not None
            n = rubros.apply_rule(conn, args.user, args.counterparty, args.category)
            if args.as_json:
                _emit_json({"rule": _jsonable(rule), "applied": n})
                return 0
            _say(f"«{rule['counterparty']}» → {rule['category']} · {n} pagos actualizados")
            return 0
        if args.counterparty or args.category:
            _say("hacen falta --counterparty y --category juntos", err=True)
            return 2
        rows = rubros.list_rules(conn, args.user)
    if args.as_json:
        _emit_json([_jsonable(r) for r in rows])
        return 0
    if not rows:
        _say("sin reglas de rubro")
    for r in rows:
        nota = f"  ({r['note']})" if r.get("note") else ""
        _say(f"  {r['counterparty']} → {r['category']} [{r['source']}]{nota}")
    return 0


def _cmd_recategorize(args: argparse.Namespace) -> int:
    from memex.modules.finance import rubros

    only = tuple(x.strip() for x in args.only.split(",") if x.strip())
    st = asyncio.run(rubros.recategorize(args.user, only=only, dry_run=args.dry_run))
    if args.as_json:
        _emit_json(
            {
                "grupos": st.grupos,
                "movimientos": st.movimientos,
                "por_regla": st.por_regla,
                "deterministas": st.deterministas,
                "por_modelo": st.por_modelo,
                "sin_cambio": st.sin_cambio,
                "consolidados_cambiados": st.consolidados_cambiados,
                "llamadas": st.llamadas,
                "cost_usd": round(st.cost_usd, 6),
                "dry_run": st.dry_run,
                "decisiones": st.decisiones,
            }
        )
        return 0
    for d in st.decisiones:
        quien = d["contraparte"] or f"(sin contraparte) {d['descripcion']}"
        via = f" [{d['por']} {d['confianza']}]" if d["por"] else ""
        _say(f"  {quien} · {d['sentido']} · {d['movimientos']} mov. → {d['rubro']}{via}")
    _say(
        f"{'(dry-run) ' if st.dry_run else ''}{st.grupos} grupos / {st.movimientos} mov.: "
        f"{st.por_regla} por regla, {st.deterministas} deterministas, {st.por_modelo} por "
        f"modelo, {st.sin_cambio} siguen en otros · {st.consolidados_cambiados} pagos "
        f"actualizados · {st.llamadas} llamadas, USD {st.cost_usd:.4f}"
    )
    return 0


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


def _cmd_dedup(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_dedup_phase2(args.user, limit=args.limit, pair_ids=args.pairs))
    _say(
        f"\nfinance dedup F2: pares={stats.pairs} confirmados={stats.confirmed} "
        f"rechazados={stats.rejected} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _fmt_pair_change(c: PairChange, rows: dict[int, DedupRow]) -> str:
    a, b = rows.get(c.a_id), rows.get(c.b_id)
    if a is None or b is None:
        return f"#{c.a_id} vs #{c.b_id}  ({c.status}, {c.reason})"

    def day(r: DedupRow) -> str:
        return r.occurred_at.astimezone(LOCAL_TZ).strftime("%Y-%m-%d")

    return (
        f"#{c.a_id} vs #{c.b_id}  {a.amount} {a.currency}  "
        f"{a.counterparty or '(sin contraparte)'} | {b.counterparty or '(sin contraparte)'}  "
        f"{day(a)} | {day(b)}  ({c.status}, {c.reason} {c.score:.2f})"
    )


def _cmd_remark(args: argparse.Namespace) -> int:
    stats = remark_phase1(args.user, dry_run=args.dry_run)
    tag = " [dry-run: nada escrito]" if stats.dry_run else ""
    _say(
        f"\nfinance remark{tag}: filas={stats.rows} pares={stats.pairs_now} "
        f"agregados={len(stats.added)} quitados={len(stats.removed)} "
        f"actualizados={stats.updated} respetados(juez)={stats.kept_decided}\n"
    )
    for c in stats.removed:
        _say(f"  - quita   {_fmt_pair_change(c, stats.rows_by_id)}\n")
    for c in stats.added:
        _say(f"  + agrega  {_fmt_pair_change(c, stats.rows_by_id)}\n")
    if args.json:
        _say(
            json.dumps(
                {
                    "rows": stats.rows,
                    "pairs_now": stats.pairs_now,
                    "added": len(stats.added),
                    "removed": len(stats.removed),
                    "updated": stats.updated,
                    "kept_decided": stats.kept_decided,
                    "dry_run": stats.dry_run,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return 0


def _cmd_consolidate(args: argparse.Namespace) -> int:
    stats = run_consolidation(args.user)
    _say(
        f"\nfinance consolidate: grupos={stats.groups} consolidados={stats.consolidated} "
        f"merges={stats.merges} huerfanos={stats.orphans}\n"
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
        status=args.status,
        kind=args.kind,
    )


def _cmd_register(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = register_from_args(conn, args.user, args)
    if args.as_json:
        _emit_json(row)
    else:
        cp = f" · {row['counterparty']}" if row["counterparty"] else ""
        nature = _nature_suffix(row)
        if row.get("already_registered"):
            _say(
                f"ya estaba registrada: {row['direction']} {row['amount']} {row['currency']}{cp}"
                f"{nature} se fundió con el pago #{row['consolidated_id']} (no se duplicó)"
            )
        else:
            _say(
                f"registrada #{row['id']}: {row['direction']} {row['amount']} {row['currency']}"
                f"{cp}{nature} (consolidado #{row['consolidated_id']})"
            )
    return 0


def _nature_suffix(row: dict[str, Any]) -> str:
    """' · reembolso' / ' · fallida' cuando la fila no es el caso común (pago completado)."""
    parts = []
    if row.get("kind", "pago") != "pago":
        parts.append(str(row["kind"]))
    if row.get("status", "completada") != "completada":
        parts.append(str(row["status"]))
    return "".join(f" · {p}" for p in parts)


def _print_detail(detail: dict[str, Any]) -> None:
    """Bloque humano del pago consolidado (estilo `memex calendario show`)."""
    _say(
        f"\npago #{detail['id']}: {detail['direction']} {detail['amount']} "
        f"{detail['currency']} · {detail['category']}{_nature_suffix(detail)}"
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


def _add_filter_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    p.add_argument("--q", default=None, help="Texto en contraparte, descripción o lugar.")
    p.add_argument("--category", default=None, help="Rubro (comida, transporte, software, …).")
    p.add_argument("--direction", default=None, choices=("ingreso", "egreso"))
    p.add_argument(
        "--status",
        default="completada",
        choices=("completada", "fallida", "pendiente", "all"),
        help="Solo plata que se movió por default; 'all' incluye fallidas/pendientes.",
    )
    p.add_argument("--since", type=date.fromisoformat, default=None, help="Desde (YYYY-MM-DD).")
    p.add_argument(
        "--until", type=date.fromisoformat, default=None, help="Hasta (YYYY-MM-DD, exclusivo)."
    )


def _cmd_subs(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = subs_mod.list_subscriptions(
            conn, args.user, include_inactive=args.include_inactive, limit=args.limit
        )
        totals = subs_mod.monthly_commitment(conn, args.user)
    if args.as_json:
        _emit_json({"count": len(rows), "items": rows, "monthly_commitment": totals})
        return 0
    if not rows:
        _say(
            f"\nSin suscripciones registradas (user {args.user}). "
            "Corré `memex-finance subs-detect` para deducirlas de los cargos.\n"
        )
        return 0
    _say(f"\nSuscripciones (user {args.user}), por próxima renovación:")
    for r in rows:
        amount = "-"
        if r["amount_typical"] is not None:
            amount = (
                "~" if r["amount_varies"] else ""
            ) + f"{r['amount_typical']:,.2f} {r['currency']}"
        when = str(r["next_renewal_on"] or "?")
        if r["days_until"] is not None:
            when += f" (en {r['days_until']} d)" if r["days_until"] >= 0 else " (vencida)"
        _say(f"  [{r['id']}] {r['provider']}  {amount}  {r['cadence_human']}")
        _say(
            f"        renueva: {when} · {r['status']} · "
            f"confianza {r['confidence']} · {r['evidence']}"
        )
    for t in totals:
        _say(
            f"\n  compromiso mensual: {t['monthly']:,.2f} {t['currency']} "
            f"({t['subscriptions']} activas)"
        )
    _say("")
    return 0


def _cmd_subs_detect(args: argparse.Namespace) -> int:
    with connection() as conn:
        stats = subs_mod.detect(conn, args.user)
    payload = {
        "providers": stats.providers,
        "detected": stats.detected,
        "created": stats.created,
        "updated": stats.updated,
        "skipped_user_owned": stats.skipped_user_owned,
    }
    if args.as_json:
        _emit_json(payload)
        return 0
    _say(
        f"\nsuscripciones: proveedores={stats.providers} detectadas={stats.detected} "
        f"nuevas={stats.created} actualizadas={stats.updated} "
        f"respetadas={stats.skipped_user_owned}\n"
    )
    return 0


def _cmd_subs_set(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = subs_mod.set_status(conn, args.user, args.id, args.status)
    if row is None:
        _say(f"\nNo existe la suscripción id={args.id} para el user {args.user}.\n", err=True)
        return 1
    if args.as_json:
        _emit_json(row)
        return 0
    _say(f"\n[{row['id']}] {row['provider']}: {row['status']} (fijada, el detector no la pisa).\n")
    return 0


def _cmd_pending(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = pending_payments(conn, args.user, resolve_days=args.days, limit=args.limit)
    if args.as_json:
        _emit_json({"count": len(rows), "resolve_days": args.days, "items": rows})
        return 0
    if not rows:
        _say(f"\nSin pagos fallidos ni pendientes por regularizar (user {args.user}).\n")
        return 0
    _say(f"\nPagos por regularizar (user {args.user}, sin pago exitoso en {args.days} días):")
    for r in rows:
        _say(
            f"  [{r['id']}] {str(r['occurred_at'])[:10]} {r['status']:9s} "
            f"{r['amount']:>12,.2f} {r['currency']} {r['counterparty']} "
            f"(hace {r['age_days']} d)"
        )
    _say("")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = list_transactions(
            conn,
            args.user,
            q=args.q,
            category=args.category,
            direction=args.direction,
            status=args.status,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    if args.as_json:
        _emit_json({"count": len(rows), "items": rows})
        return 0
    if not rows:
        _say("\nSin pagos que cumplan el filtro.\n")
        return 0
    _say(f"\n{len(rows)} pago(s):")
    for r in rows:
        when = r["occurred_at"].strftime("%Y-%m-%d")
        sign = "-" if r["direction"] == "egreso" else "+"
        cp = r["counterparty"] or r["place"] or "-"
        nature = _nature_suffix(r)
        _say(
            f"  #{r['id']} {when} {sign}{r['amount']:,.2f} {r['currency']} · {cp} "
            f"({r['category']}){nature}"
        )
    _say("")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = summarize_transactions(
            conn,
            args.user,
            by=args.by,
            q=args.q,
            category=args.category,
            direction=args.direction,
            status=args.status,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    if args.as_json:
        _emit_json({"by": args.by, "count": len(rows), "items": rows})
        return 0
    if not rows:
        _say("\nSin pagos que cumplan el filtro.\n")
        return 0
    _say(f"\nTotales por {args.by}:")
    for r in rows:
        _say(
            f"  {r['key']}: gasto neto {r['gasto_neto']:,.2f} {r['currency']} "
            f"(egresos {r['egresos']:,.2f}, reembolsos {r['reembolsos']:,.2f}, "
            f"ingresos {r['ingresos']:,.2f}, {r['count']} pagos)"
        )
    _say("")
    return 0


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


def _cmd_rm(args: argparse.Namespace) -> int:
    try:
        with connection() as conn:
            out = remove_transaction(conn, args.user, args.id)
    except ValueError as e:
        _say(str(e), err=True)
        return 1
    if args.as_json:
        _emit_json(out)
    else:
        _say(
            f"borrado el pago #{out['removed']} ({out['amount']:.0f} {out['currency']} "
            f"{out['counterparty'] or 'sin contraparte'}; {len(out['transactions'])} cruda(s))."
        )
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
        if args.cmd == "remark":
            return _cmd_remark(args)
        if args.cmd == "geo":
            return _cmd_geo(args)
        if args.cmd == "register":
            return _cmd_register(args)
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "pending":
            return _cmd_pending(args)
        if args.cmd == "subs":
            return _cmd_subs(args)
        if args.cmd == "subs-detect":
            return _cmd_subs_detect(args)
        if args.cmd == "subs-set":
            return _cmd_subs_set(args)
        if args.cmd == "summary":
            return _cmd_summary(args)
        if args.cmd == "show":
            return _cmd_show(args)
        if args.cmd == "set-place":
            return _cmd_set_place(args)
        if args.cmd == "rm":
            return _cmd_rm(args)
        if args.cmd == "rubro":
            return _cmd_rubro(args)
        if args.cmd == "recategorize":
            return _cmd_recategorize(args)
        log.error("finance.cli.unknown_command", cmd=args.cmd)
        return 1
    except Exception as e:
        log.exception("finance.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
