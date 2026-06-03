"""CLI `memex-identidades` — sync de Google Contacts + gestión de la lista de interés.

Subcomandos:
  sync         — una pasada de sync (ingress) de una cuenta de proveedor (Google People).
  add-account  — registra (upsert) una cuenta de proveedor, vinculada a la cuenta del dashboard
                 (`accounts.id`) cuyo vault tiene el token Google.
  accounts     — lista las cuentas de proveedor de un user.
  interest     — `add` / `list` / `remove` de la lista manual de organizaciones/productos/agentes.

La AUTORIZACIÓN de Google NO se hace acá: se conecta la cuenta desde el dashboard (/cuenta), que
guarda el token cifrado en el vault (Decisión 6). Server-side: habla con la DB vía `connection()`,
igual que `memex-calendar-sync`. Necesita la DB (MEMEX_DATABASE_*); para `sync`, la master key del
vault (MEMEX_SECRET_KEY), inyectada por `doppler run`.

Exit code 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv
from sqlalchemy import text

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.identidades.providers import known_providers
from memex.modules.identidades.providers.base import ContactsProviderError
from memex.modules.identidades.sync import run_sync

_ORG_KINDS = ("organizacion", "producto", "agente")


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows)."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-identidades")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sync_p = sub.add_parser("sync", help="Sincroniza (ingress) una cuenta de contactos externa.")
    sync_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    sync_p.add_argument("--account", type=int, required=True, help="Id de la cuenta de proveedor.")
    sync_p.add_argument(
        "--full", action="store_true", help="Ignora el cursor y trae todo (full resync)."
    )

    add_p = sub.add_parser("add-account", help="Registra (upsert) una cuenta de proveedor.")
    add_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    add_p.add_argument("--provider", default="google", help=f"Proveedor ({known_providers()}).")
    add_p.add_argument("--label", required=True, help="Etiqueta visible (ej. email de la cuenta).")
    add_p.add_argument(
        "--account-id",
        type=int,
        required=True,
        help="Id de la cuenta del dashboard (accounts.id) cuyo vault tiene el token Google.",
    )

    acc_p = sub.add_parser("accounts", help="Lista las cuentas de proveedor de un user.")
    acc_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    int_p = sub.add_parser("interest", help="Gestiona la lista de identidades de interés (orgs).")
    int_sub = int_p.add_subparsers(dest="interest_cmd", required=True)

    iadd = int_sub.add_parser("add", help="Agrega una org/producto/agente a la lista de interés.")
    iadd.add_argument("--user", type=int, default=1, help="User id (default 1).")
    iadd.add_argument("--name", required=True, help="Nombre canónico (ej. 'Unity', 'Claude').")
    iadd.add_argument(
        "--kind",
        choices=_ORG_KINDS,
        default="organizacion",
        help="Sub-tipo (default organizacion).",
    )
    iadd.add_argument(
        "--alias", action="append", default=[], help="Alias (repetible). Ej. --alias claude.ai."
    )
    iadd.add_argument(
        "--domain", action="append", default=[], help="Dominio de email (repetible). Ej. unity.com."
    )
    iadd.add_argument("--description", default="", help="Descripción corta opcional.")

    ilist = int_sub.add_parser("list", help="Lista la lista de interés de un user.")
    ilist.add_argument("--user", type=int, default=1, help="User id (default 1).")

    irm = int_sub.add_parser("remove", help="Quita una org de la lista de interés por id.")
    irm.add_argument("--user", type=int, default=1, help="User id (default 1).")
    irm.add_argument("--id", type=int, required=True, help="Id de la org a quitar.")

    return parser


def _cmd_sync(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_sync(args.user, args.account, full=args.full))
    _say(
        f"\nidentidades sync: pulled={stats.pulled} created={stats.created} "
        f"modified={stats.modified} deleted={stats.deleted} unchanged={stats.unchanged} "
        f"errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_add_account(args: argparse.Namespace) -> int:
    if args.provider not in known_providers():
        _say(
            f"\nProveedor desconocido {args.provider!r}. Conocidos: {known_providers()}\n", err=True
        )
        return 1
    with connection() as conn:
        owner = conn.execute(
            text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": args.account_id}
        ).scalar()
        if owner != args.user:
            _say(
                f"\nLa cuenta del dashboard id={args.account_id} no existe o no es del user "
                f"{args.user}.\n",
                err=True,
            )
            return 1
        account_id = conn.execute(
            text(
                """
                INSERT INTO mod_identidades_provider_accounts
                  (user_id, provider, account_label, account_id)
                VALUES (:uid, :provider, :label, :acc)
                ON CONFLICT (user_id, provider, account_label)
                  DO UPDATE SET account_id = EXCLUDED.account_id
                RETURNING id
                """
            ),
            {
                "uid": args.user,
                "provider": args.provider,
                "label": args.label,
                "acc": args.account_id,
            },
        ).scalar_one()
    _say(
        f"\nCuenta lista: id={account_id} ({args.provider}/{args.label}), "
        f"vault accounts.id={args.account_id}."
    )
    _say(f"Sincronizar con: memex-identidades sync --account {account_id}\n")
    return 0


def _cmd_accounts(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, provider, account_label, account_id, enabled, last_sync_at,
                           sync_token IS NOT NULL AS has_cursor
                    FROM mod_identidades_provider_accounts
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
        _say(f"\nSin cuentas de contactos para el user {args.user}.\n")
        return 0
    _say(f"\nCuentas de contactos (user {args.user}):")
    for r in rows:
        _say(
            f"  [{r['id']}] {r['provider']}/{r['account_label']} vault_acc={r['account_id']} "
            f"enabled={r['enabled']} cursor={'si' if r['has_cursor'] else 'no'} "
            f"last_sync={r['last_sync_at']}"
        )
    _say("")
    return 0


def _cmd_interest_add(args: argparse.Namespace) -> int:
    with connection() as conn:
        org_id = conn.execute(
            text(
                """
                INSERT INTO mod_identidades_orgs
                  (user_id, name, kind, aliases, domains, description, interest, source)
                VALUES (:uid, :name, :kind, :aliases, :domains, :desc, TRUE, 'manual')
                ON CONFLICT (user_id, name) DO UPDATE SET
                  kind = EXCLUDED.kind, aliases = EXCLUDED.aliases, domains = EXCLUDED.domains,
                  description = EXCLUDED.description, interest = TRUE, updated_at = NOW()
                RETURNING id
                """
            ),
            {
                "uid": args.user,
                "name": args.name,
                "kind": args.kind,
                "aliases": list(args.alias),
                "domains": [d.strip().lower() for d in args.domain],
                "desc": args.description,
            },
        ).scalar_one()
    _say(f"\nEn la lista de interés: id={org_id} {args.name!r} ({args.kind}).\n")
    return 0


def _cmd_interest_list(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT id, name, kind, aliases, domains, interest
                    FROM mod_identidades_orgs
                    WHERE user_id = :uid AND interest
                    ORDER BY name
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    if not rows:
        _say(f"\nLista de interés vacía para el user {args.user}.\n")
        return 0
    _say(f"\nLista de interés (user {args.user}):")
    for r in rows:
        aliases = ", ".join(r["aliases"]) if r["aliases"] else "-"
        domains = ", ".join(r["domains"]) if r["domains"] else "-"
        _say(f"  [{r['id']}] {r['name']} ({r['kind']}) aliases=[{aliases}] domains=[{domains}]")
    _say("")
    return 0


def _cmd_interest_remove(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = conn.execute(
            text(
                "DELETE FROM mod_identidades_orgs WHERE id = :id AND user_id = :uid RETURNING name"
            ),
            {"id": args.id, "uid": args.user},
        ).first()
    if row is None:
        _say(f"\nNo existe la org id={args.id} para el user {args.user}.\n", err=True)
        return 1
    _say(f"\nQuitada de la lista: {row[0]!r} (id={args.id}).\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.identidades.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    log.info("identidades.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "sync":
            return _cmd_sync(args)
        if args.cmd == "add-account":
            return _cmd_add_account(args)
        if args.cmd == "accounts":
            return _cmd_accounts(args)
        if args.cmd == "interest":
            if args.interest_cmd == "add":
                return _cmd_interest_add(args)
            if args.interest_cmd == "list":
                return _cmd_interest_list(args)
            if args.interest_cmd == "remove":
                return _cmd_interest_remove(args)
        log.error("identidades.cli.unknown_command", cmd=args.cmd)
        return 1
    except ContactsProviderError as e:
        log.error("identidades.cli.provider_error", status_code=e.status_code, msg=str(e))
        print(
            "\nERROR del proveedor de contactos. ¿Conectaste Google en /cuenta (re-consent con el "
            "scope de Contacts) y corriste con `doppler run -- ...`?\n",
            file=sys.stderr,
        )
        return 1
    except Exception as e:
        log.exception("identidades.cli.fatal", exc_type=type(e).__name__, exc_msg=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
