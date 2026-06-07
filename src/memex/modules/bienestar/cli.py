"""CLI `memex-bienestar` — registrador determinista de salud y bienestar (sin LLM).

La ENTRADA del módulo: un agente externo (p. ej. Hermes) entiende el lenguaje natural por Telegram y
llama a estos subcomandos con campos ya estructurados. memex solo guarda y reporta.

Subcomandos:
  register  — escribe un evento (categoría, actividad, descripción, fecha, detail JSON).
  list      — lista registros filtrados (período / categoría / actividad).
  summary   — agregado para reportes (total + conteos por categoría y actividad).
  habit     — gestiona hábitos (add/list/rm): compromisos recurrentes.
  adherence — adherencia + rachas de los hábitos activos.
  help      — resumen de los comandos (para que el agente descubra la CLI).

Todos aceptan `--json` para salida que parsea el agente; sin él, la salida es humana. Server-side:
habla con la DB vía `connection()`. Exit 0 si OK, 1 si error.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from dotenv import load_dotenv

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.bienestar.habits import add_habit, adherence, delete_habit, list_habits
from memex.modules.bienestar.module import list_registros, register, summary


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola (cp1252 en Windows): evita que `print()`
    reviente con acentos."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: Any) -> None:
    # `default=str` serializa datetimes (isoformat vía str) sin acoplar a un encoder propio.
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


def _parse_when(s: str | None) -> tuple[datetime | None, str | None]:
    """Parsea `--occurred-at` / `--since` / `--until`. Solo fecha (YYYY-MM-DD) → precisión 'date'
    (medianoche UTC); con hora → 'datetime' (naive=UTC). Vacío → (None, None)."""
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


def _since_from_args(args: argparse.Namespace) -> datetime | None:
    """`--days N` (últimos N días) tiene prioridad; si no, `--since`."""
    if getattr(args, "days", None):
        return datetime.now(UTC) - timedelta(days=int(args.days))
    since, _ = _parse_when(args.since)
    return since


_HELP = """memex-bienestar — salud y bienestar (registrador determinista, sin LLM).

Comandos:
  register    registra un evento (comida/higiene/ejercicio/grooming/salud/otros)
  list        lista registros filtrados
  summary     agregado: total + conteos por categoría/actividad
  habit       define hábitos: add | list | rm
  adherence   adherencia + rachas de los hábitos activos
  help        muestra esta ayuda

Reglas:
  --event <id>  hechos del MISMO mensaje comparten el id → el grafo los conecta
                (un mensaje con un solo hecho no necesita --event)
  --json        la respuesta JSON es la ÚLTIMA línea de stdout (las previas son logs)

Flags de cada comando: memex-bienestar <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-bienestar")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("help", help="Resumen de los comandos (para descubrir la CLI).")

    reg = sub.add_parser("register", help="Registra un evento de bienestar (campos estructurados).")
    reg.add_argument("--user", type=int, default=1, help="User id (default 1).")
    reg.add_argument(
        "--category", required=True, help="comida|higiene|ejercicio|grooming|salud|otros."
    )
    reg.add_argument("--activity", default="", help="Acto concreto (ej. 'almuerzo', 'gimnasio').")
    reg.add_argument("--description", default="", help="Detalle libre.")
    reg.add_argument(
        "--occurred-at", default=None, help="ISO 8601 (fecha o fecha-hora). Sin esto, ahora."
    )
    reg.add_argument("--detail", default=None, help="Objeto JSON con campos extra (ej. calorías).")
    reg.add_argument(
        "--source-text", default=None, help="El mensaje NL original del agente (procedencia)."
    )
    reg.add_argument(
        "--event", default=None, help="Id de correlación: hechos del mismo mensaje lo comparten."
    )
    reg.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    lst = sub.add_parser("list", help="Lista registros filtrados, más nuevos primero.")
    lst.add_argument("--user", type=int, default=1)
    lst.add_argument("--since", default=None, help="ISO 8601: desde (inclusive).")
    lst.add_argument("--until", default=None, help="ISO 8601: hasta (exclusive).")
    lst.add_argument("--days", type=int, default=None, help="Atajo: últimos N días.")
    lst.add_argument("--category", default=None)
    lst.add_argument("--activity", default=None)
    lst.add_argument("--limit", type=int, default=100)
    lst.add_argument("--json", action="store_true", dest="as_json")

    summ = sub.add_parser("summary", help="Agregado (total + conteos) para reportes.")
    summ.add_argument("--user", type=int, default=1)
    summ.add_argument("--since", default=None)
    summ.add_argument("--until", default=None)
    summ.add_argument("--days", type=int, default=None)
    summ.add_argument("--json", action="store_true", dest="as_json")

    hab = sub.add_parser("habit", help="Gestiona hábitos (compromisos recurrentes).")
    hsub = hab.add_subparsers(dest="habit_cmd", required=True)
    hadd = hsub.add_parser("add", help="Crea un hábito.")
    hadd.add_argument("--user", type=int, default=1)
    hadd.add_argument("--name", required=True)
    hadd.add_argument("--cadence", required=True, choices=("daily", "weekly"))
    hadd.add_argument("--target", type=int, default=1, help="Meta por período (default 1).")
    hadd.add_argument("--activity", default="", help="Actividad que cuenta (clave de match).")
    hadd.add_argument("--category", default=None, help="O una categoría (si no hay actividad).")
    hadd.add_argument("--json", action="store_true", dest="as_json")
    hlist = hsub.add_parser("list", help="Lista hábitos.")
    hlist.add_argument("--user", type=int, default=1)
    hlist.add_argument("--all", action="store_true", help="Incluir inactivos.")
    hlist.add_argument("--json", action="store_true", dest="as_json")
    hrm = hsub.add_parser("rm", help="Borra un hábito.")
    hrm.add_argument("--user", type=int, default=1)
    hrm.add_argument("--id", type=int, required=True)

    adh = sub.add_parser("adherence", help="Adherencia y rachas de los hábitos activos.")
    adh.add_argument("--user", type=int, default=1)
    adh.add_argument("--periods", type=int, default=14, help="Períodos a mirar (default 14).")
    adh.add_argument("--tz", default="America/Bogota", help="TZ IANA del bucket.")
    adh.add_argument("--json", action="store_true", dest="as_json")

    return parser


def _cmd_register(args: argparse.Namespace) -> int:
    occurred_at, precision = _parse_when(args.occurred_at)
    detail: dict[str, Any] | None = None
    if args.detail:
        parsed = json.loads(args.detail)
        if not isinstance(parsed, dict):
            _say("--detail debe ser un objeto JSON", err=True)
            return 1
        detail = parsed
    metadata = {"source_text": args.source_text} if args.source_text else None
    with connection() as conn:
        row = register(
            conn,
            args.user,
            category=args.category,
            activity=args.activity,
            description=args.description,
            occurred_at=occurred_at,
            precision=precision,
            detail=detail,
            metadata=metadata,
            event_id=args.event,
        )
    if args.as_json:
        _emit_json(row)
    else:
        _say(
            f"registrado #{row['id']}: {row['category']} · {row['activity']} ({row['occurred_at']})"
        )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    since = _since_from_args(args)
    until, _ = _parse_when(args.until)
    with connection() as conn:
        rows = list_registros(
            conn,
            args.user,
            since=since,
            until=until,
            category=args.category,
            activity=args.activity,
            limit=args.limit,
        )
    if args.as_json:
        _emit_json(rows)
        return 0
    if not rows:
        _say("(sin registros)")
    for r in rows:
        desc = f" — {r['description']}" if r["description"] else ""
        _say(f"#{r['id']} {r['occurred_at']} [{r['category']}] {r['activity']}{desc}")
    return 0


def _cmd_summary(args: argparse.Namespace) -> int:
    since = _since_from_args(args)
    until, _ = _parse_when(args.until)
    with connection() as conn:
        s = summary(conn, args.user, since=since, until=until)
    if args.as_json:
        _emit_json(s)
        return 0
    _say(f"Total: {s['total']}")
    if s["by_category"]:
        _say("Por categoría:")
        for cat, n in s["by_category"].items():
            _say(f"  {cat}: {n}")
    if s["by_activity"]:
        _say("Por actividad:")
        for act, n in s["by_activity"].items():
            _say(f"  {act}: {n}")
    return 0


def _cmd_habit(args: argparse.Namespace) -> int:
    with connection() as conn:
        if args.habit_cmd == "add":
            row = add_habit(
                conn,
                args.user,
                name=args.name,
                cadence=args.cadence,
                target_count=args.target,
                activity=args.activity,
                category=args.category,
            )
            if args.as_json:
                _emit_json(row)
            else:
                _say(
                    f"hábito #{row['id']}: {row['name']} ({row['cadence']} x{row['target_count']})"
                )
            return 0
        if args.habit_cmd == "list":
            habits = list_habits(conn, args.user, include_inactive=args.all)
            if args.as_json:
                _emit_json(habits)
                return 0
            if not habits:
                _say("(sin hábitos)")
            for h in habits:
                clave = h["activity"] or f"cat:{h['category']}"
                _say(f"#{h['id']} {h['name']} — {h['cadence']} x{h['target_count']} [{clave}]")
            return 0
        # rm
        ok = delete_habit(conn, args.user, args.id)
        _say("borrado" if ok else f"no existe el hábito #{args.id}", err=not ok)
        return 0 if ok else 1


def _cmd_adherence(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = adherence(conn, args.user, tz=args.tz, periods=args.periods)
    if args.as_json:
        _emit_json(rows)
        return 0
    if not rows:
        _say("(sin hábitos activos)")
    for a in rows:
        estado = "ok" if a["met_current"] else ".."
        _say(
            f"[{estado}] {a['habit']['name']}: {a['current']}/{a['target_count']} este período "
            f"· racha {a['streak']}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.bienestar.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "help":
        _say(_HELP)
        return 0
    log.info("bienestar.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "register":
            return _cmd_register(args)
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "summary":
            return _cmd_summary(args)
        if args.cmd == "habit":
            return _cmd_habit(args)
        if args.cmd == "adherence":
            return _cmd_adherence(args)
        log.error("bienestar.cli.unknown_command", cmd=args.cmd)
        return 1
    except Exception as e:
        log.exception("bienestar.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
