"""CLI `memex-calendar-sync` — sync de calendarios externos hacia el dominio `mod_calendar_events`.

Subcomandos:
  pull         — una pasada de sync (ingress) de una cuenta de proveedor (Google).
  authorize    — flujo OAuth interactivo (browser) de una cuenta: pide el set COMPLETO de scopes
                 de memex (decisión 6) y persiste el token. One-time.
  add-account  — registra una cuenta de proveedor (upsert) para un user.
  accounts     — lista las cuentas de proveedor de un user.

Server-side (corre DENTRO de memex, no en el cliente local): habla con la DB vía `connection()` y
con el proveedor vía httpx, igual que `memex-ocr`/`memex-extract`. Necesita la DB (MEMEX_DATABASE_*)
y, para `pull`/`authorize`, las env vars de OAuth (path al token y al client_secret), inyectadas
por `doppler run`.

Exit code 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.calendar.consolidate import run_consolidation
from memex.modules.calendar.dedup_llm import run_dedup_phase2
from memex.modules.calendar.merge_llm import run_merge
from memex.modules.calendar.providers import known_providers, oauth
from memex.modules.calendar.providers.base import CalendarProviderError
from memex.modules.calendar.sync import run_pull, run_push

#: Env var (nombre) con el path al client_secret.json de Google (OAuth Desktop App).
_DEFAULT_CLIENT_SECRET_ENV = "GOOGLE_CLIENT_SECRET_PATH"
#: Env var (nombre) por default con el path al archivo del token OAuth de la cuenta.
_DEFAULT_TOKEN_ENV = "GOOGLE_CALENDAR_TOKEN_PATH"


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows), como los CLIs
    de social/telegram: evita que `print()` reviente o ensucie con acentos/guiones."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


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
        "--token-path-env",
        default=_DEFAULT_TOKEN_ENV,
        help=f"NOMBRE de la env var con el path al token OAuth (default {_DEFAULT_TOKEN_ENV}).",
    )
    add_p.add_argument(
        "--write-back",
        action="store_true",
        help="Activa el write-back (push) a esta cuenta desde el arranque.",
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
        "conflicts", help="Lista los conflictos pendientes de revisión (choques de alta prioridad)."
    )
    conf_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    acc_p = sub.add_parser("accounts", help="Lista las cuentas de proveedor de un user.")
    acc_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

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
                "SELECT provider, token_path_env FROM mod_calendar_provider_accounts "
                "WHERE id = :id AND user_id = :uid"
            ),
            {"id": args.account, "uid": args.user},
        ).first()
    if row is None:
        _say(f"\nNo existe la cuenta id={args.account} para el user {args.user}.\n", err=True)
        return 1
    provider, token_env = str(row[0]), str(row[1])

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
        f"errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_dedup(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_dedup_phase2(args.user, limit=args.limit))
    _say(
        f"\ncalendar dedup F2: pares={stats.pairs} confirmados={stats.confirmed} "
        f"rechazados={stats.rejected} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_consolidate(args: argparse.Namespace) -> int:
    stats = run_consolidation(args.user)
    _say(
        f"\ncalendar consolidate: grupos={stats.groups} consolidados={stats.consolidated} "
        f"merges={stats.merges} ecos={stats.echoes} conflictos={stats.conflicts}\n"
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
                    SELECT cf.id, ca.title AS a_title, ca.starts_on AS a_date,
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


def _cmd_add_account(args: argparse.Namespace) -> int:
    if args.provider not in known_providers():
        _say(
            f"\nProveedor desconocido {args.provider!r}. Conocidos: {known_providers()}\n",
            err=True,
        )
        return 1
    with connection() as conn:
        account_id = conn.execute(
            text(
                """
                INSERT INTO mod_calendar_provider_accounts
                  (user_id, provider, account_label, calendar_id, token_path_env, write_back)
                VALUES (:uid, :provider, :label, :calendar_id, :token_env, :wb)
                ON CONFLICT (user_id, provider, account_label, calendar_id)
                  DO UPDATE SET token_path_env = EXCLUDED.token_path_env,
                                write_back = EXCLUDED.write_back
                RETURNING id
                """
            ),
            {
                "uid": args.user,
                "provider": args.provider,
                "label": args.label,
                "calendar_id": args.calendar_id,
                "token_env": args.token_path_env,
                "wb": args.write_back,
            },
        ).scalar_one()
    wb_msg = "con write-back ON" if args.write_back else "solo lectura (write_back OFF)"
    _say(f"\nCuenta lista: id={account_id} ({args.provider}/{args.label}) - {wb_msg}.")
    _say(f"Token en la env var: {args.token_path_env}")
    _say(f"Autorizar con: memex-calendar-sync authorize --account {account_id}\n")
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
                           enabled, write_back, last_sync_at, sync_token IS NOT NULL AS has_cursor
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
        if args.cmd == "dedup":
            return _cmd_dedup(args)
        if args.cmd == "consolidate":
            return _cmd_consolidate(args)
        if args.cmd == "merge":
            return _cmd_merge(args)
        if args.cmd == "conflicts":
            return _cmd_conflicts(args)
        if args.cmd == "accounts":
            return _cmd_accounts(args)
        log.error("calendar.cli.unknown_command", cmd=args.cmd)
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
