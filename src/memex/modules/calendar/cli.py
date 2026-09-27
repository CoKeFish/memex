"""CLI `memex-calendar-sync` — el dominio calendar por consola: sync + CRUD de eventos.

Subcomandos del AGENTE (expuestos como `memex calendario …`, contrato `--json` = ÚLTIMA línea):
  add / list / show / update / rm — CRUD de eventos sobre la capa consolidada (manual.py).
  conflicts    — conflictos de horario pendientes de revisión.
  recheck      — (mantenimiento) FASE 1 sobre todo el histórico con la compuerta vigente.

Subcomandos de MANTENIMIENTO (solo por esta CLI, bloqueados en la superficie del agente):
  pull         — una pasada de sync (ingress) de una cuenta de proveedor (Google).
  push         — write-back (egress) de la vista consolidada a una cuenta write_back, según
                 su política (`set-push-policy`: estados que salen + piso temporal).
  authorize    — flujo OAuth interactivo (browser) de una cuenta LEGACY (token en archivo): pide
                 el set COMPLETO de scopes de memex (decisión 6) y persiste el token. One-time.
                 Con `--account-id` no hace falta: el token sale del vault (/cuenta).
  add-account  — registra una cuenta de proveedor (upsert) para un user; `--account-id N` la
                 liga a la cuenta del dashboard que ya conectó Google.
  accounts     — lista las cuentas de proveedor de un user.
  dedup / consolidate / merge — pasos del ciclo (FASE 2 LLM / proyección / enriquecido).

Server-side (corre DENTRO de memex, no en el cliente local): habla con la DB vía `connection()` y
con el proveedor vía httpx, igual que `memex-ocr`/`memex-extract`. Necesita la DB (MEMEX_DATABASE_*)
y, para `pull`/`authorize`, las env vars de OAuth (path al token y al client_secret), inyectadas
por `doppler run`.

Exit code 0 si OK; 1 si error fatal; 2 si error de uso.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, time
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.calendar.commitment import LABEL as COMMITMENT_LABEL
from memex.modules.calendar.commitment import run_classify
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.dedup_llm import run_dedup_phase2
from memex.modules.calendar.health import sync_health
from memex.modules.calendar.manual import (
    EventChanges,
    ManualEventError,
    add_event,
    cancel_instance,
    list_events,
    remove_event,
    set_commitment_level,
    set_protected,
    show_event,
    split_series,
    update_event,
    update_instance,
)
from memex.modules.calendar.merge_llm import run_merge
from memex.modules.calendar.module import recheck_candidates
from memex.modules.calendar.providers import known_providers, oauth
from memex.modules.calendar.providers.base import CalendarProviderError
from memex.modules.calendar.settings import (
    asiste_includes_declined,
    llm_on_past_events,
    protection_rules,
    set_asiste_includes_declined,
    set_llm_on_past_events,
    set_protection_rules,
)
from memex.modules.calendar.sync import run_pull, run_push

#: Env var (nombre) con el path al client_secret.json de Google (OAuth Desktop App).
_DEFAULT_CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET_PATH"
#: Env var (nombre) por default con el path al archivo del token OAuth de la cuenta.
_DEFAULT_TOKEN_ENV = "GOOGLE_CALENDAR_TOKEN_PATH"
#: Estados que puede escribir el write-back (vocabulario de `commitment`, 0094).
_PUSH_COMMITMENTS = ("confirmado", "provisional", "anuncio")
_PUSH_PAST_ALL = "todo"
#: Centinela para «no tocar» un campo opcional de set-push-policy.
_UNSET = object()


def _parse_push_commitments(raw: str) -> list[str]:
    """`confirmado,provisional` → lista validada contra el vocabulario (orden canónico)."""
    wanted = {p.strip().lower() for p in raw.split(",") if p.strip()}
    bad = sorted(wanted - set(_PUSH_COMMITMENTS))
    if bad or not wanted:
        raise argparse.ArgumentTypeError(
            f"estados desconocidos {bad}; válidos: {', '.join(_PUSH_COMMITMENTS)}"
        )
    return [c for c in _PUSH_COMMITMENTS if c in wanted]


def _parse_push_past_days(raw: str) -> int | None:
    """`0` = desde hoy, `N` = desde hace N días, `todo` = sin piso (todo el histórico)."""
    if raw.strip().lower() == _PUSH_PAST_ALL:
        return None
    try:
        n = int(raw)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"esperaba un entero >= 0 o '{_PUSH_PAST_ALL}'") from e
    if n < 0:
        raise argparse.ArgumentTypeError("esperaba un entero >= 0")
    return n


def _push_policy_label(commitments: list[str] | tuple[str, ...], past_days: int | None) -> str:
    since = (
        "todo el histórico"
        if past_days is None
        else ("desde hoy" if past_days == 0 else f"desde hace {past_days} d")
    )
    return f"sale: {', '.join(commitments)} · {since}"


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows), como los CLIs
    de social/telegram: evita que `print()` reviente o ensucie con acentos/guiones."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: object) -> None:
    """Contrato del agente: la respuesta JSON es la ÚLTIMA línea de stdout."""
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


_HELP_AGENT = """memex-calendar-sync — eventos del calendario (capa consolidada).

Comandos del agente (también como `memex calendario <comando>`):
  add     --title T --date YYYY-MM-DD [--time HH:MM] [--end-time HH:MM] [--end-date YYYY-MM-DD]
          [--location L] [--description D] [--fijado]
          [--every daily|weekly|monthly|yearly [--byday MO,TU,..] [--interval N]
           [--until YYYY-MM-DD | --count N]]   recurrente = UN maestro con regla (sin fin si no
          hay until/count); las ocurrencias se expanden al leer (modelo de Google Calendar)
  list    [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--limit N]   eventos de la ventana; los
          recurrentes expandidos (una fila por ocurrencia, mismo id + series_id)
  show    <id>                                       detalle: fuentes, regla, próximas ocurrencias
  update  <id> [--title|--date|--time|--end-time|--end-date|--location|--description ...]
          [--series] [--instance YYYY-MM-DD [--following]]   corrige el evento; en un
          recurrente, toda la serie; --instance = solo esa ocurrencia (excepción);
          --instance --following = esa y las siguientes (parte la serie en dos)
  rm      <id> [--series] [--instance YYYY-MM-DD [--following]]   borra el evento/la serie;
          --instance cancela solo esa ocurrencia (EXDATE); --following termina la serie ahí
  estado <id> confirmado|provisional|anuncio        tu palabra sobre el estado del evento
          (confirmado = inscrito/pagó/confirmó; provisional = participante sin confirmar;
          anuncio = convocatoria o evento abierto, no es un compromiso tuyo)
  fijar <id> [--off]                                fija el evento: gana la consolidación, nada
          lo sobrescribe y cuenta para los conflictos (en un recurrente, toda la serie)
  fijar-regla add|rm|list [--match T]               regla: lo que contenga T (título/lugar/
          descripción) queda fijado en cada consolidación
  conflicts                                          conflictos de horario pendientes
  sync-status                                        ¿la sync con Google está funcionando?

Reglas:
  --json        la respuesta JSON es la ÚLTIMA línea de stdout
  update/rm     operan sobre el id CONSOLIDADO (el que muestran list/show)
  rm            borra del calendario de memex; en Google solo borra las copias que memex
                escribió ahí (lo que creaste directo en Google no se toca)

Detalle de un comando: memex-calendar-sync <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-calendar-sync")
    sub = parser.add_subparsers(dest="cmd", required=True)

    pull_p = sub.add_parser("pull", help="Sincroniza (ingress) una cuenta de calendario externo.")
    pull_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    pull_p.add_argument("--account", type=int, required=True, help="Id de la cuenta de proveedor.")
    pull_p.add_argument(
        "--full", action="store_true", help="Ignora el cursor y trae todo (full resync)."
    )
    pull_p.add_argument(
        "--past-days",
        type=int,
        default=None,
        help="Ventana hacia atrás del full sync, en días (default 183 ≈ 6 meses).",
    )
    pull_p.add_argument(
        "--future-days",
        type=int,
        default=None,
        help="Ventana hacia adelante del full sync, en días (default 365 ≈ 12 meses).",
    )

    push_p = sub.add_parser(
        "push", help="Write-back: empuja la vista consolidada a una cuenta write_back."
    )
    push_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    push_p.add_argument("--account", type=int, required=True, help="Id de la cuenta de proveedor.")

    auth_p = sub.add_parser("authorize", help="Flujo OAuth interactivo de una cuenta (one-time).")
    auth_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    auth_p.add_argument("--account", type=int, required=True, help="Id de la cuenta a autorizar.")
    auth_p.add_argument(
        "--client-secret-env",
        default=_DEFAULT_CLIENT_SECRET_ENV,
        help=f"Env var con el path al client_secret.json (default {_DEFAULT_CLIENT_SECRET_ENV}).",
    )

    add_p = sub.add_parser("add-account", help="Registra (upsert) una cuenta de proveedor.")
    add_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    add_p.add_argument("--provider", required=True, help=f"Proveedor ({known_providers()}).")
    add_p.add_argument("--label", required=True, help="Etiqueta visible (ej. email de la cuenta).")
    add_p.add_argument("--calendar-id", default="primary", help="Calendar id (default 'primary').")
    add_p.add_argument(
        "--account-id",
        type=int,
        default=None,
        help=(
            "Id de la cuenta del dashboard (accounts.id) que ya conectó Google en /cuenta: el "
            "token sale de su vault y no hace falta `authorize`."
        ),
    )
    add_p.add_argument(
        "--token-path-env",
        default=_DEFAULT_TOKEN_ENV,
        help=(
            "Legacy (sin --account-id): NOMBRE de la env var con el path al token OAuth "
            f"(default {_DEFAULT_TOKEN_ENV})."
        ),
    )
    add_p.add_argument(
        "--write-back",
        action="store_true",
        help="Activa el write-back (push) a esta cuenta desde el arranque.",
    )
    add_p.add_argument(
        "--push-commitments",
        type=_parse_push_commitments,
        default=["confirmado"],
        help="Estados que salen al proveedor, separados por coma (default: confirmado).",
    )
    add_p.add_argument(
        "--push-past-days",
        type=_parse_push_past_days,
        default=0,
        help="Piso: 0 = desde hoy, N = desde hace N días, 'todo' = sin piso (default 0).",
    )

    spp_p = sub.add_parser(
        "set-push-policy", help="Qué estados salen al proveedor y desde cuándo (write-back)."
    )
    spp_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    spp_p.add_argument("--account", type=int, required=True, help="Id de la cuenta.")
    spp_p.add_argument(
        "--commitments",
        type=_parse_push_commitments,
        default=None,
        help="Estados separados por coma (confirmado,provisional,anuncio).",
    )
    spp_p.add_argument(
        "--past-days",
        type=_parse_push_past_days,
        default=_UNSET,
        help="0 = desde hoy, N = desde hace N días, 'todo' = sin piso.",
    )

    swb_p = sub.add_parser(
        "set-write-back", help="Activa/desactiva el write-back (push) de una cuenta."
    )
    swb_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    swb_p.add_argument("--account", type=int, required=True, help="Id de la cuenta.")
    swb_p.add_argument("--off", action="store_true", help="Desactiva (default: activa).")

    dedup_p = sub.add_parser(
        "dedup", help="Dedup FASE 2: resuelve con LLM los pares candidatos (confirmar/rechazar)."
    )
    dedup_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    dedup_p.add_argument("--limit", type=int, default=200, help="Máximo de pares (default 200).")
    dedup_p.add_argument(
        "--include-past",
        action="store_true",
        help="Juzga también los pares vencidos en esta corrida (sin tocar la perilla llm_on_past).",
    )

    recheck_p = sub.add_parser(
        "recheck",
        help="Dedup FASE 1 sobre TODO el histórico con la compuerta vigente: registra los pares "
        "candidatos que faltaban (los ya decididos no se tocan).",
    )
    recheck_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    cons_p = sub.add_parser(
        "consolidate", help="Reconstruye la vista consolidada (grupos + ganador por prioridad)."
    )
    cons_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    merge_p = sub.add_parser(
        "merge", help="Enriquece con LLM los consolidados multi-copia (suma info extra)."
    )
    merge_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    merge_p.add_argument("--limit", type=int, default=200, help="Máximo de consolidados (def 200).")

    conf_p = sub.add_parser(
        "conflicts", help="Lista los conflictos de horario pendientes de revisión."
    )
    conf_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    conf_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    acc_p = sub.add_parser("accounts", help="Lista las cuentas de proveedor de un user.")
    acc_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    # --- CRUD de eventos (superficie del agente: `memex calendario …`) --------------- #
    sub.add_parser("help", help="Resumen de los comandos del agente.")

    add_p = sub.add_parser(
        "add", help="Crea un evento manual (o uno RECURRENTE con --every [--byday] [--until])."
    )
    add_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    add_p.add_argument("--title", required=True, help="Título del evento.")
    add_p.add_argument("--date", required=True, type=date.fromisoformat, help="Fecha YYYY-MM-DD.")
    add_p.add_argument(
        "--time", type=time.fromisoformat, default=None, help="Hora HH:MM (sin esto: todo el día)."
    )
    add_p.add_argument("--end-time", type=time.fromisoformat, default=None, help="Hora fin HH:MM.")
    add_p.add_argument(
        "--end-date", type=date.fromisoformat, default=None, help="Última fecha (multi-día)."
    )
    add_p.add_argument("--location", default="", help="Lugar (texto libre).")
    add_p.add_argument("--description", default="", help="Detalle libre.")
    add_p.add_argument(
        "--fijado",
        dest="protected",
        action="store_true",
        help="Fijado: nada lo sobrescribe al consolidar y cuenta para los conflictos.",
    )
    add_p.add_argument(
        "--every",
        choices=("daily", "weekly", "monthly", "yearly"),
        default=None,
        help="Recurrente: UN evento maestro con regla (las ocurrencias se expanden al leer).",
    )
    add_p.add_argument(
        "--byday",
        default=None,
        help="Días de la semana (con --every weekly): MO,TU,WE,TH,FR,SA,SU o lunes,martes,…",
    )
    add_p.add_argument(
        "--interval",
        type=int,
        default=1,
        help="Cada N (cada 2 semanas = --every weekly --interval 2).",
    )
    add_p.add_argument(
        "--until",
        type=date.fromisoformat,
        default=None,
        help="Última fecha de la serie (inclusive). Sin --until ni --count: sin fin.",
    )
    add_p.add_argument("--count", type=int, default=None, help="Cantidad de ocurrencias.")
    add_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    list_p = sub.add_parser("list", help="Próximos eventos (capa consolidada), cronológicos.")
    list_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    list_p.add_argument(
        "--since", type=date.fromisoformat, default=None, help="Desde (default hoy)."
    )
    list_p.add_argument("--until", type=date.fromisoformat, default=None, help="Hasta (inclusive).")
    list_p.add_argument("--limit", type=int, default=20, help="Máximo de eventos (default 20).")
    list_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    show_p = sub.add_parser("show", help="Detalle de un evento: fuentes, serie y conflictos.")
    show_p.add_argument("consolidated_id", type=int, help="Id consolidado (el de list/show).")
    show_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    show_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    upd_p = sub.add_parser("update", help="Corrige un evento (o toda la serie con --series).")
    upd_p.add_argument("consolidated_id", type=int, help="Id consolidado (el de list/show).")
    upd_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    upd_p.add_argument("--title", default=None, help="Nuevo título.")
    upd_p.add_argument("--date", type=date.fromisoformat, default=None, help="Nueva fecha.")
    upd_p.add_argument("--time", type=time.fromisoformat, default=None, help="Nueva hora.")
    upd_p.add_argument("--end-time", type=time.fromisoformat, default=None, help="Nueva hora fin.")
    upd_p.add_argument(
        "--end-date", type=date.fromisoformat, default=None, help="Nueva última fecha."
    )
    upd_p.add_argument("--location", default=None, help="Nuevo lugar.")
    upd_p.add_argument("--description", default=None, help="Nuevo detalle.")
    upd_p.add_argument(
        "--series",
        action="store_true",
        help="Aplica a TODAS las instancias de la serie (no admite --date/--end-date).",
    )
    upd_p.add_argument(
        "--instance",
        type=date.fromisoformat,
        default=None,
        help="Solo ESA ocurrencia de un evento recurrente (YYYY-MM-DD): crea una excepción.",
    )
    upd_p.add_argument(
        "--following",
        action="store_true",
        help="Con --instance: esa ocurrencia Y LAS SIGUIENTES (parte la serie en dos).",
    )
    upd_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    rm_p = sub.add_parser("rm", help="Borra un evento (o la serie con --series). Definitivo.")
    rm_p.add_argument("consolidated_id", type=int, help="Id consolidado (el de list/show).")
    rm_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rm_p.add_argument(
        "--series", action="store_true", help="Borra TODAS las instancias de la serie."
    )
    rm_p.add_argument(
        "--instance",
        type=date.fromisoformat,
        default=None,
        help="Cancela solo ESA ocurrencia de un evento recurrente (YYYY-MM-DD); la serie sigue.",
    )
    rm_p.add_argument(
        "--following",
        action="store_true",
        help="Con --instance: termina la serie desde esa ocurrencia (inclusive).",
    )
    rm_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    pr_p = sub.add_parser(
        "fijar",
        help="Fija un evento (o toda su serie): gana la consolidación, nada lo sobrescribe "
        "y cuenta para los conflictos de horario. --off lo libera.",
    )
    pr_p.add_argument("consolidated_id", type=int, help="Id consolidado (el de list/show).")
    pr_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    pr_p.add_argument("--off", action="store_true", help="Libera el evento (deja de estar fijado).")
    pr_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    cm_p = sub.add_parser(
        "estado",
        help="Corrige el estado de un evento (confirmado | provisional | anuncio): tu palabra "
        "sobre lo que dijo el texto.",
    )
    cm_p.add_argument("consolidated_id", type=int, help="Id consolidado (el de list/show).")
    cm_p.add_argument("level", choices=("confirmado", "provisional", "anuncio"))
    cm_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    cm_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    cls_p = sub.add_parser(
        "classify",
        help="(mantenimiento) Juez codex: pone estado a los eventos extraídos que no lo tienen, "
        "leyendo su mensaje fuente.",
    )
    cls_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    cls_p.add_argument(
        "--since-days", type=int, default=30, help="Desde N días atrás (default 30)."
    )
    cls_p.add_argument("--limit", type=int, default=200, help="Máximo de eventos (default 200).")

    rule_p = sub.add_parser(
        "fijar-regla",
        help="Reglas de fijado automático: todo evento cuyo título/lugar/descripción contenga "
        "el texto queda fijado en cada consolidación (add | rm | list).",
    )
    rule_p.add_argument("action", choices=("add", "rm", "list"))
    rule_p.add_argument("--match", default=None, help="Texto a buscar (p. ej. 'Britano').")
    rule_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    rule_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    ss_p = sub.add_parser(
        "sync-status", help="¿La sincronización con el proveedor está funcionando?"
    )
    ss_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    ss_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    slp_p = sub.add_parser(
        "set-llm-past",
        help="¿Gastar LLM (dedup F2 + merge) en eventos ya vencidos? Default: off (no gasta).",
    )
    slp_p.add_argument("mode", choices=("on", "off"), help="on = procesa pasados; off = no gasta.")
    slp_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    slp_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    sad_p = sub.add_parser(
        "set-asiste-declined",
        help="¿Un invitado que rechazó cuenta como «asiste»? Default: off (rechazar ≠ asistir).",
    )
    sad_p.add_argument("mode", choices=("on", "off"), help="on = cuenta; off = no cuenta.")
    sad_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    sad_p.add_argument("--json", action="store_true", dest="as_json", help="Salida JSON.")

    return parser


def _cmd_pull(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_pull(
            args.user,
            args.account,
            full=args.full,
            past_days=args.past_days,
            future_days=args.future_days,
        )
    )
    _say(
        f"\ncalendar sync: pulled={stats.pulled} created={stats.created} "
        f"modified={stats.modified} deleted={stats.deleted} unchanged={stats.unchanged} "
        f"dedup_pairs={stats.dedup_pairs} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_authorize(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = conn.execute(
            text(
                "SELECT provider, token_path_env, account_id FROM mod_calendar_provider_accounts "
                "WHERE id = :id AND user_id = :uid"
            ),
            {"id": args.account, "uid": args.user},
        ).first()
    if row is None:
        _say(f"\nNo existe la cuenta id={args.account} para el user {args.user}.\n", err=True)
        return 1
    if row[2] is not None:
        _say(
            f"\nLa cuenta {args.account} usa el token Google del vault de la cuenta del dashboard "
            f"#{row[2]} (Google conectado en /cuenta): no hay nada que autorizar por consola.\n"
            "Si Google revocó el acceso, «Reconectar» en /cuenta.\n"
        )
        return 0
    provider, token_env = str(row[0]), (str(row[1]) if row[1] is not None else None)

    # client_secret: env var si está seteada, si no el default repo-local secrets/.
    cs_env = os.environ.get(args.client_secret_env, "").strip()
    cs_path = cs_env or str(oauth.default_client_secret_path())
    if not Path(cs_path).exists():
        _say(
            f"\nNo encuentro el client_secret en:\n  {cs_path}\n"
            f"Poné ahí tu client_secret.json (Desktop App), o seteá {args.client_secret_env}.\n",
            err=True,
        )
        return 1
    # token: default repo-local secrets/ (la env var, si existe, lo overridea).
    token_path = oauth.resolve_token_path(provider, args.account, token_env)

    _say("\nIniciando flujo OAuth2 - se abrira el navegador para consentimiento.")
    _say(f"  proveedor:     {provider}")
    _say(f"  client_secret: {cs_path}")
    _say(f"  destino token: {token_path}")
    _say("  scopes:        Gmail full + Calendar read/write (un solo consentimiento)\n")
    oauth.authorize(provider, client_secret_path=cs_path, token_path=token_path)
    _say(f"\nTokens guardados en {token_path}. El refresh_token se renueva solo cada corrida.\n")
    return 0


def _cmd_push(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_push(args.user, args.account))
    _say(
        f"\ncalendar push: consolidados={stats.consolidated} creados={stats.created} "
        f"actualizados={stats.updated} borrados={stats.deleted} saltados={stats.skipped} "
        f"fuera_de_politica={stats.filtered} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_recheck(args: argparse.Namespace) -> int:
    with connection() as conn:
        stats = recheck_candidates(conn, args.user)
    _say(
        f"\ncalendar recheck: eventos={stats['events']} pares={stats['pairs']} "
        f"candidatos_nuevos={stats['new_candidates']}\n"
    )
    return 0


def _cmd_dedup(args: argparse.Namespace) -> int:
    stats = asyncio.run(
        run_dedup_phase2(
            args.user, limit=args.limit, include_past=True if args.include_past else None
        )
    )
    _say(
        f"\ncalendar dedup F2: pares={stats.pairs} confirmados={stats.confirmed} "
        f"rechazados={stats.rejected} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_consolidate(args: argparse.Namespace) -> int:
    stats = run_consolidation(args.user)
    _say(
        f"\ncalendar consolidate: grupos={stats.groups} consolidados={stats.consolidated} "
        f"merges={stats.merges} ecos={stats.echoes} huerfanos={stats.orphans} "
        f"conflictos={stats.conflicts}\n"
    )
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_merge(args.user, limit=args.limit))
    _say(
        f"\ncalendar merge: consolidados={stats.consolidated} enriquecidos={stats.merged} "
        f"sin-cambio={stats.skipped} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_conflicts(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT cf.id, cf.consolidated_a_id, cf.consolidated_b_id,
                           ca.title AS a_title, ca.starts_on AS a_date,
                           ca.start_time AS a_time, cb.title AS b_title, cb.starts_on AS b_date,
                           cb.start_time AS b_time
                    FROM mod_calendar_conflicts cf
                    JOIN mod_calendar_consolidated ca ON ca.id = cf.consolidated_a_id
                    JOIN mod_calendar_consolidated cb ON cb.id = cf.consolidated_b_id
                    WHERE cf.user_id = :uid AND cf.status = 'pending'
                    ORDER BY cf.id
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    if getattr(args, "as_json", False):
        _emit_json(
            {
                "items": [
                    {
                        "conflict_id": int(r["id"]),
                        "a_id": int(r["consolidated_a_id"]),
                        "a_title": r["a_title"],
                        "a_starts_on": r["a_date"],
                        "a_start_time": r["a_time"],
                        "b_id": int(r["consolidated_b_id"]),
                        "b_title": r["b_title"],
                        "b_starts_on": r["b_date"],
                        "b_start_time": r["b_time"],
                    }
                    for r in rows
                ],
                "count": len(rows),
            }
        )
        return 0
    if not rows:
        _say(f"\nSin conflictos pendientes para el user {args.user}.\n")
        return 0
    _say(f"\nConflictos pendientes de revision (user {args.user}):")
    for r in rows:
        _say(
            f"  [{r['id']}] {r['a_title']!r} ({r['a_date']} {r['a_time']}) "
            f"CHOCA CON {r['b_title']!r} ({r['b_date']} {r['b_time']})"
        )
    _say("")
    return 0


# --- CRUD de eventos (superficie del agente) ----------------------------------------- #


def _fmt_when(item: dict[str, object]) -> str:
    """`2026-06-20 09:00-10:00` / `2026-06-20 (todo el día)` / `2026-06-20 - 2026-06-22`."""
    when = str(item["starts_on"])
    if item.get("ends_on"):
        when += f" - {item['ends_on']}"
    if item.get("start_time"):
        when += f" {item['start_time']}"
        if item.get("end_time"):
            when += f"-{item['end_time']}"
    else:
        when += " (todo el día)"
    return when


def _cmd_add(args: argparse.Namespace) -> int:
    result = add_event(
        args.user,
        title=args.title,
        starts_on=args.date,
        ends_on=args.end_date,
        start_time=args.time,
        end_time=args.end_time,
        location=args.location,
        description=args.description,
        protected=args.protected,
        every=args.every,
        until=args.until,
        byday=[d for d in (args.byday or "").replace(";", ",").split(",") if d.strip()],
        interval=args.interval,
        count=args.count,
    )
    cons_id = result["consolidated_ids"][0] if result["consolidated_ids"] else "?"
    if result["recurrence"]:
        _say(
            f'Evento recurrente creado: [{cons_id}] "{result["title"]}" — desde {args.date}, '
            f"{result['recurrence_human']}."
        )
    else:
        _say(f'Evento creado: [{cons_id}] "{result["title"]}" — {args.date}.')
    if result["dedup_pairs"]:
        _say(
            "OJO: quedó marcado como posible duplicado de un evento existente "
            "(par pendiente de revisión)."
        )
    if args.as_json:
        _emit_json(result)
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    since = args.since if args.since is not None else date.today()
    items = list_events(args.user, since=since, until=args.until, limit=args.limit)
    if args.as_json:
        _emit_json({"items": items, "count": len(items), "since": since.isoformat()})
        return 0
    if not items:
        _say(f"\nSin eventos desde {since} (user {args.user}).\n")
        return 0
    _say(f"\nPróximos eventos desde {since} (user {args.user}):")
    for it in items:
        loc = f" ({it['location']})" if it["location"] else ""
        serie = (
            f" ·serie {it['recurrence_human']}"
            if it.get("recurrence_human")
            else (" ·serie" if it["recurring"] else "")
        )
        fuentes = f" ·{it['member_count']} fuentes" if int(it["member_count"]) > 1 else ""
        nivel = (
            f" ·{COMMITMENT_LABEL.get(str(it.get('commitment')), '')}"
            if it.get("commitment")
            else ""
        )
        _say(f'  [{it["id"]}] {_fmt_when(it)} "{it["title"]}"{loc}{nivel}{serie}{fuentes}')
    _say("")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    detail = show_event(args.user, args.consolidated_id)
    if args.as_json:
        _emit_json(detail)
        return 0
    _say(f'\n[{detail["id"]}] "{detail["title"]}" — {_fmt_when(detail)}')
    if detail["location"]:
        _say(f"  lugar:       {detail['location']}")
    if detail["place"]:
        place = detail["place"]
        _say(f"  lugar resuelto: {place['name']} — {place['formatted_address']}")
    if detail["description"]:
        _say(f"  descripción: {detail['description']}")
    if detail.get("commitment"):
        ev = f" — «{detail['commitment_evidence']}»" if detail.get("commitment_evidence") else ""
        _say(
            f"  estado:      {COMMITMENT_LABEL.get(detail['commitment'], detail['commitment'])}{ev}"
        )
    if detail.get("recurrence_human"):
        _say(f"  recurrencia: {detail['recurrence_human']}")
        if detail.get("next_occurrences"):
            _say(f"  próximas:    {', '.join(detail['next_occurrences'])}")
    if detail.get("original_start_on"):
        _say(f"  excepción de la ocurrencia del {detail['original_start_on']}")
    if detail["series_id"]:
        _say(f"  serie:       {detail['series_id']}")
    _say(f"  fuentes ({len(detail['members'])}):")
    for m in detail["members"]:
        tags = [str(m["origin"])]
        if m["provider"]:
            tags.append(str(m["provider"]))
        if m["is_winner"]:
            tags.append("principal")
        if m["cancelled"]:
            tags.append("cancelado")
        inbox = (
            f" inbox={','.join(str(i) for i in m['source_inbox_ids'])}"
            if m["source_inbox_ids"]
            else ""
        )
        _say(f"    - evento #{m['event_id']} [{' · '.join(tags)}]{inbox}")
    if detail["pending_conflicts"]:
        _say("  conflictos pendientes:")
        for cf in detail["pending_conflicts"]:
            _say(
                f'    - conflicto con [{cf["with_id"]}] "{cf["with_title"]}" '
                f"({cf['with_starts_on']})"
            )
    _say("")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    changes = EventChanges(
        title=args.title,
        starts_on=args.date,
        ends_on=args.end_date,
        start_time=args.time,
        end_time=args.end_time,
        location=args.location,
        description=args.description,
    )
    if changes.empty():
        _say("nada que actualizar: pasá al menos un campo a corregir.", err=True)
        return 2
    if args.series and (args.date is not None or args.end_date is not None):
        _say(
            "--date/--end-date no se combinan con --series (cada instancia tiene su fecha).",
            err=True,
        )
        return 2
    if args.following and args.instance is None:
        _say("--following requiere --instance YYYY-MM-DD.", err=True)
        return 2
    if args.instance is not None and args.following:
        result = split_series(args.user, args.consolidated_id, args.instance, changes)
        _say(
            f"Serie [{args.consolidated_id}] partida desde {args.instance}: la vieja queda "
            f"{result.get('old_recurrence_human', '')}; la nueva arranca ese día."
        )
        if args.as_json:
            _emit_json(result)
        return 0
    if args.instance is not None:
        result = update_instance(args.user, args.consolidated_id, args.instance, changes)
        _say(f"Ocurrencia del {args.instance} de [{args.consolidated_id}]: {result['mode']}.")
        if args.as_json:
            _emit_json(result)
        return 0
    result = update_event(args.user, args.consolidated_id, changes, series=args.series)
    if result["instances"] > 1:
        _say(f"Actualizada la serie: {result['instances']} instancias.")
    else:
        _say(f"Actualizado [{args.consolidated_id}].")
    if args.as_json:
        _emit_json(result)
    return 0


def _cmd_protect(args: argparse.Namespace) -> int:
    result = set_protected(args.user, args.consolidated_id, not args.off)
    what = "la serie" if result["recurring"] else "el evento"
    if result["protected"]:
        _say(
            f'Fijado {what} [{args.consolidated_id}] "{result["title"]}": nada lo sobrescribe y '
            "cuenta para los conflictos."
        )
    else:
        _say(f'Liberado {what} [{args.consolidated_id}] "{result["title"]}" (ya no está fijado).')
    if args.as_json:
        _emit_json(result)
    return 0


def _cmd_commitment(args: argparse.Namespace) -> int:
    result = set_commitment_level(args.user, args.consolidated_id, args.level)
    _say(
        f'[{args.consolidated_id}] "{result["title"]}" → {COMMITMENT_LABEL[result["commitment"]]}.'
    )
    if args.as_json:
        _emit_json(result)
    return 0


def _cmd_classify(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_classify(args.user, since_days=args.since_days, limit=args.limit))
    _say(
        f"\ncalendar classify: juzgados={stats.judged} confirmado={stats.confirmado} "
        f"provisional={stats.provisional} anuncio={stats.anuncio} errores={stats.errors}\n"
    )
    if stats.judged:
        run_consolidation(args.user)
    return 1 if stats.errors else 0


def _cmd_protect_rule(args: argparse.Namespace) -> int:
    with connection() as conn:
        rules = protection_rules(conn, args.user)
        if args.action in ("add", "rm"):
            if not args.match or not args.match.strip():
                _say("--match es obligatorio para add/rm.", err=True)
                return 2
            key = " ".join(args.match.split())
            if args.action == "add":
                if key.lower() not in {r.lower() for r in rules}:
                    rules.append(key)
            else:
                rules = [r for r in rules if r.lower() != key.lower()]
            set_protection_rules(conn, args.user, rules)
    marked = 0
    if args.action == "add":
        marked = run_consolidation(args.user).protected_by_rules
    if args.as_json:
        _emit_json({"rules": rules, "marked": marked})
        return 0
    if args.action == "add":
        _say(f"Regla agregada: {args.match!r}. Eventos fijados ahora: {marked}.")
    elif args.action == "rm":
        _say(f"Regla quitada: {args.match!r} (los ya fijados siguen fijados).")
    if rules:
        _say("Reglas de fijado: " + "; ".join(rules))
    else:
        _say("Sin reglas de protección.")
    return 0


def _cmd_rm(args: argparse.Namespace) -> int:
    if args.following and args.instance is None:
        _say("--following requiere --instance YYYY-MM-DD.", err=True)
        return 2
    if args.instance is not None and args.following:
        result = split_series(args.user, args.consolidated_id, args.instance, None)
        _say(
            f"Serie [{args.consolidated_id}] terminada desde {args.instance}: queda "
            f"{result.get('old_recurrence_human', '')}."
        )
        if args.as_json:
            _emit_json(result)
        return 0
    if args.instance is not None:
        result = cancel_instance(args.user, args.consolidated_id, args.instance)
        _say(
            f"Cancelada la ocurrencia del {args.instance} de [{args.consolidated_id}]; "
            f"la serie sigue ({result['recurrence_human']})."
        )
        if args.as_json:
            _emit_json(result)
        return 0
    result = remove_event(args.user, args.consolidated_id, series=args.series)
    if result["instances"] > 1:
        _say(f'Borrada la serie "{result["title"]}": {result["instances"]} instancias.')
    else:
        _say(f'Borrado [{args.consolidated_id}] "{result["title"]}".')
    if args.as_json:
        _emit_json(result)
    return 0


_OVERALL_LABEL = {
    "ok": "OK — funcionando",
    "desactualizado": "DESACTUALIZADO",
    "error": "ERROR — la última sincronización falló",
    "nunca": "NUNCA sincronizó",
    "sin_cuentas": "SIN CUENTAS de proveedor",
}

_CURSOR_LABEL = {
    "incremental": "al día (incremental)",
    "full_resync_pendiente": "hará una sync completa",
    "sin_primera_sync": "sin primera sync",
}


def _age_str(hours: float | None) -> str:
    if hours is None:
        return "nunca corrió"
    if hours < 1:
        return f"hace {int(hours * 60)} min"
    if hours < 48:
        return f"hace {hours:.0f} h"
    return f"hace {hours / 24:.0f} días"


def _cmd_set_llm_past(args: argparse.Namespace) -> int:
    value = args.mode == "on"
    with connection() as conn:
        set_llm_on_past_events(conn, args.user, value)
    if value:
        _say("LLM en eventos pasados: PRENDIDO — dedup F2 y merge también juzgan lo vencido.")
        _say("Los pares/grupos que quedaron salteados se retoman en la próxima corrida.")
    else:
        _say("LLM en eventos pasados: APAGADO — dedup F2 y merge no gastan en lo vencido.")
    if args.as_json:
        _emit_json({"llm_on_past_events": value})
    return 0


def _cmd_set_asiste_declined(args: argparse.Namespace) -> int:
    value = args.mode == "on"
    with connection() as conn:
        set_asiste_includes_declined(conn, args.user, value)
    if value:
        _say("Asiste incluye declined: PRENDIDO — quien rechazó la invitación recibe «asiste».")
        _say("Las nuevas aristas aparecen en la próxima consolidación (el organizador no cambia).")
    else:
        _say("Asiste incluye declined: APAGADO — quien rechazó NO recibe «asiste».")
        _say("Las «asiste» de invitados que rechazaron se quitan en el próximo reconcile_graph.")
    if args.as_json:
        _emit_json({"asiste_includes_declined": value})
    return 0


def _cmd_sync_status(args: argparse.Namespace) -> int:
    with connection() as conn:
        data = sync_health(conn, args.user)
        llm_past = llm_on_past_events(conn, args.user)
        asiste_decl = asiste_includes_declined(conn, args.user)
    if args.as_json:
        _emit_json({**data, "asiste_includes_declined": asiste_decl})
        return 0
    _say(f"\nSincronización de calendario (user {args.user}):")
    _say(
        "  LLM en eventos pasados: "
        + (
            "PRENDIDO (también juzga lo vencido)."
            if llm_past
            else "APAGADO (no gasta en lo vencido)."
        )
    )
    _say(
        "  Asiste incluye declined: "
        + ("SÍ (quien rechazó asiste)." if asiste_decl else "NO (rechazar no es asistir).")
    )
    ages = [
        a["last_pull_age_hours"]
        for a in data["accounts"]
        if a["enabled"] and a["last_pull_age_hours"] is not None
    ]
    estado = _OVERALL_LABEL.get(str(data["overall"]), str(data["overall"]))
    if ages:
        estado += f" — última bajada desde el proveedor {_age_str(min(ages))}."
    _say(f"  Estado: {estado}")
    if data["auto_sync_active"]:
        _say("  Sync automática: ACTIVA (el scheduler corre el ciclo de calendar).")
    else:
        _say("  Sync automática: APAGADA — los datos solo se actualizan a mano.")
    for a in data["accounts"]:
        flags = []
        if not a["enabled"]:
            flags.append("deshabilitada")
        if a["write_back"]:
            flags.append("write-back ON (escribe en el proveedor)")
        extra = f" [{' · '.join(flags)}]" if flags else ""
        _say(f"  [{a['account_id']}] {a['provider']}/{a['account_label']}{extra}")
        pull_status = f" — {a['last_pull_status']}" if a["last_pull_status"] else ""
        _say(
            f"      bajada (pull): {_age_str(a['last_pull_age_hours'])}{pull_status} · "
            f"cursor: {_CURSOR_LABEL.get(str(a['cursor_state']), a['cursor_state'])}"
        )
        push_when = (
            "nunca corrió"
            if a["last_push_at"] is None
            else f"{a['last_push_at']} — {a['last_push_status']}"
        )
        _say(f"      subida (push): {push_when}")
    if data["accounts"]:
        first = data["accounts"][0]["account_id"]
        _say(f"Para bajar ahora: memex-calendar-sync pull --account {first}\n")
    else:
        _say("Conectá una cuenta con: memex-calendar-sync add-account\n")
    return 0


def _cmd_add_account(args: argparse.Namespace) -> int:
    if args.provider not in known_providers():
        _say(
            f"\nProveedor desconocido {args.provider!r}. Conocidos: {known_providers()}\n",
            err=True,
        )
        return 1
    with connection() as conn:
        if args.account_id is not None:
            owner = conn.execute(
                text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": args.account_id}
            ).scalar()
            if owner is None or int(owner) != args.user:
                _say(
                    f"\nLa cuenta del dashboard id={args.account_id} no existe o no es del user "
                    f"{args.user}.\n",
                    err=True,
                )
                return 1
        account_id = conn.execute(
            text(
                """
                INSERT INTO mod_calendar_provider_accounts
                  (user_id, provider, account_label, calendar_id, token_path_env, write_back,
                   push_commitments, push_past_days, account_id)
                VALUES (:uid, :provider, :label, :calendar_id, :token_env, :wb,
                        CAST(:pc AS TEXT[]), :pd, :acct)
                ON CONFLICT (user_id, provider, account_label, calendar_id)
                  DO UPDATE SET token_path_env = EXCLUDED.token_path_env,
                                write_back = EXCLUDED.write_back,
                                push_commitments = EXCLUDED.push_commitments,
                                push_past_days = EXCLUDED.push_past_days,
                                account_id = EXCLUDED.account_id
                RETURNING id
                """
            ),
            {
                "uid": args.user,
                "provider": args.provider,
                "label": args.label,
                "calendar_id": args.calendar_id,
                "token_env": None if args.account_id is not None else args.token_path_env,
                "wb": args.write_back,
                "pc": args.push_commitments,
                "pd": args.push_past_days,
                "acct": args.account_id,
            },
        ).scalar_one()
    wb_msg = "con write-back ON" if args.write_back else "solo lectura (write_back OFF)"
    _say(f"\nCuenta lista: id={account_id} ({args.provider}/{args.label}) - {wb_msg}.")
    _say(f"Write-back: {_push_policy_label(args.push_commitments, args.push_past_days)}")
    if args.account_id is not None:
        _say(
            f"Token: vault de la cuenta del dashboard #{args.account_id} (Google conectado en "
            "/cuenta); no hace falta authorize.\n"
        )
    else:
        _say(f"Token en la env var: {args.token_path_env}")
        _say(f"Autorizar con: memex-calendar-sync authorize --account {account_id}\n")
    return 0


def _cmd_set_push_policy(args: argparse.Namespace) -> int:
    touch_pd = args.past_days is not _UNSET
    if args.commitments is None and not touch_pd:
        _say("\nNada que cambiar: pasá --commitments y/o --past-days.\n", err=True)
        return 2
    with connection() as conn:
        row = conn.execute(
            text(
                """
                UPDATE mod_calendar_provider_accounts
                SET push_commitments = COALESCE(CAST(:pc AS TEXT[]), push_commitments),
                    push_past_days = CASE WHEN :touch_pd THEN :pd ELSE push_past_days END
                WHERE id = :id AND user_id = :uid
                RETURNING push_commitments, push_past_days
                """
            ),
            {
                "pc": args.commitments,
                "touch_pd": touch_pd,
                "pd": args.past_days if touch_pd else None,
                "id": args.account,
                "uid": args.user,
            },
        ).first()
    if row is None:
        _say(f"\nNo existe la cuenta id={args.account} para el user {args.user}.\n", err=True)
        return 1
    _say(f"\nCuenta {args.account}: {_push_policy_label(list(row[0]), row[1])}.\n")
    return 0


def _cmd_set_write_back(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = conn.execute(
            text(
                "UPDATE mod_calendar_provider_accounts SET write_back = :wb "
                "WHERE id = :id AND user_id = :uid RETURNING id"
            ),
            {"wb": not args.off, "id": args.account, "uid": args.user},
        ).first()
    if row is None:
        _say(f"\nNo existe la cuenta id={args.account} para el user {args.user}.\n", err=True)
        return 1
    _say(f"\nCuenta {args.account}: write_back = {'OFF' if args.off else 'ON'}.\n")
    return 0


def _cmd_accounts(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, provider, account_label, calendar_id, token_path_env,
                           enabled, write_back, last_sync_at, sync_token IS NOT NULL AS has_cursor,
                           push_commitments, push_past_days, account_id
                    FROM mod_calendar_provider_accounts
                    WHERE user_id = :uid
                    ORDER BY id
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    if not rows:
        _say(f"\nSin cuentas de calendario para el user {args.user}.\n")
        return 0
    _say(f"\nCuentas de calendario (user {args.user}):")
    for r in rows:
        _say(
            f"  [{r['id']}] {r['provider']}/{r['account_label']} cal={r['calendar_id']} "
            f"enabled={r['enabled']} write_back={r['write_back']} "
            f"cursor={'si' if r['has_cursor'] else 'no'} last_sync={r['last_sync_at']}"
        )
        token_src = (
            f"vault de la cuenta #{r['account_id']}"
            if r["account_id"] is not None
            else f"archivo ({r['token_path_env']})"
        )
        _say(f"      token: {token_src}")
        if r["write_back"]:
            _say(f"      {_push_policy_label(list(r['push_commitments']), r['push_past_days'])}")
    _say("")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.calendar.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    log.info("calendar.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "pull":
            return _cmd_pull(args)
        if args.cmd == "push":
            return _cmd_push(args)
        if args.cmd == "authorize":
            return _cmd_authorize(args)
        if args.cmd == "add-account":
            return _cmd_add_account(args)
        if args.cmd == "set-write-back":
            return _cmd_set_write_back(args)
        if args.cmd == "set-push-policy":
            return _cmd_set_push_policy(args)
        if args.cmd == "dedup":
            return _cmd_dedup(args)
        if args.cmd == "recheck":
            return _cmd_recheck(args)
        if args.cmd == "consolidate":
            return _cmd_consolidate(args)
        if args.cmd == "merge":
            return _cmd_merge(args)
        if args.cmd == "conflicts":
            return _cmd_conflicts(args)
        if args.cmd == "accounts":
            return _cmd_accounts(args)
        if args.cmd == "help":
            _say(_HELP_AGENT)
            return 0
        if args.cmd == "add":
            return _cmd_add(args)
        if args.cmd == "list":
            return _cmd_list(args)
        if args.cmd == "show":
            return _cmd_show(args)
        if args.cmd == "update":
            return _cmd_update(args)
        if args.cmd == "rm":
            return _cmd_rm(args)
        if args.cmd == "fijar":
            return _cmd_protect(args)
        if args.cmd == "estado":
            return _cmd_commitment(args)
        if args.cmd == "classify":
            return _cmd_classify(args)
        if args.cmd == "fijar-regla":
            return _cmd_protect_rule(args)
        if args.cmd == "sync-status":
            return _cmd_sync_status(args)
        if args.cmd == "set-llm-past":
            return _cmd_set_llm_past(args)
        if args.cmd == "set-asiste-declined":
            return _cmd_set_asiste_declined(args)
        log.error("calendar.cli.unknown_command", cmd=args.cmd)
        return 1
    except ManualEventError as e:
        _say(str(e), err=True)
        return 1
    except CalendarProviderError as e:
        log.error("calendar.cli.provider_error", status_code=e.status_code, msg=str(e))
        print(
            "\nERROR del proveedor de calendario. ¿Autorizaste la cuenta "
            "(memex-calendar-sync authorize) y corriste con `doppler run -- ...`?\n",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        log.exception("calendar.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
