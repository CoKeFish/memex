"""CLI `memex-identidades` — alta por tarjeta + sync de Google Contacts + interés + merges.

Subcomandos:
  add          — resolve-or-create de una identidad desde una tarjeta de contacto (lo usa el agente
                 vía `memex identidad add`): misma resolución que la extracción, sin LLM.
  help         — resumen de los comandos.
  sync         — una pasada de sync (ingress) de una cuenta de proveedor (Google People).
  add-account  — registra (upsert) una cuenta de proveedor, vinculada a la cuenta del dashboard
                 (`accounts.id`) cuyo vault tiene el token Google.
  accounts     — lista las cuentas de proveedor de un user.
  interest     — `add` / `list` / `remove` de la lista manual de organizaciones de interés.
  merge        — desempate LLM (FASE 2) de los candidatos de merge de la zona gris del difuso.
  candidates   — lista los candidatos de merge pendientes.
  backfill-productos — reclasifica orgs→producto por voto de menciones (DRY-RUN por default;
                 `--apply` escribe). Determinista, sin LLM.

La AUTORIZACIÓN de Google NO se hace acá: se conecta la cuenta desde el dashboard (/cuenta), que
guarda el token cifrado en el vault (Decisión 6). Server-side: habla con la DB vía `connection()`,
igual que `memex-calendar-sync`. `sync`/`merge` necesitan la DB y (sync) la master key del vault
(MEMEX_SECRET_KEY); `merge` usa el LLM (DEEPSEEK_API_KEY) — inyectados por `doppler run`.

Exit code 0 si OK; 1 si error fatal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.modules.identidades.backfill import apply_reclassification, find_product_candidates
from memex.modules.identidades.dedup_llm import run_merge_phase2
from memex.modules.identidades.module import register_card
from memex.modules.identidades.normalize import norm_identifier
from memex.modules.identidades.providers import known_providers
from memex.modules.identidades.providers.base import ContactsProviderError
from memex.modules.identidades.sync import run_sync


def _safe(text_: str) -> str:
    """Sanea un string para el encoding de la consola actual (cp1252 en Windows)."""
    enc = sys.stdout.encoding or "utf-8"
    return text_.encode(enc, errors="replace").decode(enc, errors="replace")


def _say(msg: str, *, err: bool = False) -> None:
    print(_safe(msg), file=sys.stderr if err else sys.stdout)


def _emit_json(obj: Any) -> None:
    """La fila pública como ÚLTIMA línea de stdout (las previas son logs). `default=str` serializa
    datetimes sin acoplar a un encoder propio (igual que bienestar/finance)."""
    print(_safe(json.dumps(obj, default=str, ensure_ascii=False)))


_HELP = """memex-identidades — directorio de personas, organizaciones y productos (resolución
determinista).

Comandos del agente:
  add          registra/resuelve una tarjeta de contacto (resolve-or-create, no duplica)
  help         muestra esta ayuda

Mantenimiento (no del agente; usar 'memex-identidades' directo, no 'memex identidad'):
  sync · add-account · accounts · interest · merge · candidates · backfill-productos

add — desde una tarjeta de contacto / vCard (la lee el agente, no memex):
  memex-identidades add --name "<nombre>" --kind <persona|organizacion|producto> [--email <e>]
      [--phone <t>] [--handle <@>] [--org "<empresa>"] [--role "<rol>"] [--json]
  - resuelve contra el directorio (señales fuertes + difuso) y crea si no existe; idempotente.
  - --org (solo personas) teje la afiliación persona↔organización.
  - --json: la fila pública es la ÚLTIMA línea de stdout (las previas son logs).

Flags de cada comando: memex-identidades <comando> -h"""


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-identidades")
    sub = parser.add_subparsers(dest="cmd", required=True)

    add_id = sub.add_parser("add", help="Registra/resuelve una identidad desde una tarjeta.")
    add_id.add_argument("--user", type=int, default=1, help="User id (default 1).")
    add_id.add_argument(
        "--name", required=True, help="Nombre de la persona, organización o producto."
    )
    add_id.add_argument(
        "--kind",
        required=True,
        choices=["persona", "organizacion", "producto"],
        help="Tipo de identidad.",
    )
    add_id.add_argument("--email", help="Email de contacto.")
    add_id.add_argument("--phone", help="Teléfono de contacto.")
    add_id.add_argument("--handle", help="Handle/usuario (ej. @ada).")
    add_id.add_argument(
        "--org", help="Empresa de la persona (teje la afiliación; solo --kind persona)."
    )
    add_id.add_argument("--role", help="Rol/cargo en la empresa (con --org).")
    add_id.add_argument(
        "--json", dest="as_json", action="store_true", help="Emite la fila pública como JSON."
    )

    sub.add_parser("help", help="Resumen de los comandos (para descubrir la CLI).")

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

    int_p = sub.add_parser("interest", help="Gestiona la lista de organizaciones de interés.")
    int_sub = int_p.add_subparsers(dest="interest_cmd", required=True)

    iadd = int_sub.add_parser("add", help="Agrega/actualiza una organización de interés.")
    iadd.add_argument("--user", type=int, default=1, help="User id (default 1).")
    iadd.add_argument("--name", required=True, help="Nombre canónico (ej. 'Unity', 'Claude').")
    iadd.add_argument(
        "--alias", action="append", default=[], help="Alias (repetible). Ej. --alias claude.ai."
    )
    iadd.add_argument(
        "--domain", action="append", default=[], help="Dominio de email (repetible). Ej. unity.com."
    )
    iadd.add_argument("--description", default="", help="Nota corta opcional.")

    ilist = int_sub.add_parser("list", help="Lista la lista de interés de un user.")
    ilist.add_argument("--user", type=int, default=1, help="User id (default 1).")

    irm = int_sub.add_parser("remove", help="Quita una identidad de interés por id.")
    irm.add_argument("--user", type=int, default=1, help="User id (default 1).")
    irm.add_argument("--id", type=int, required=True, help="Id de la identidad a quitar.")

    merge_p = sub.add_parser("merge", help="Desempate LLM de los candidatos de merge (FASE 2).")
    merge_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    merge_p.add_argument("--limit", type=int, default=200, help="Máximo de pares a resolver.")

    cand_p = sub.add_parser("candidates", help="Lista los candidatos de merge pendientes.")
    cand_p.add_argument("--user", type=int, default=1, help="User id (default 1).")

    bf_p = sub.add_parser(
        "backfill-productos",
        help="Reclasifica orgs→producto por voto de menciones (dry-run sin --apply).",
    )
    bf_p.add_argument("--user", type=int, default=1, help="User id (default 1).")
    bf_p.add_argument(
        "--apply",
        action="store_true",
        help="Aplica la reclasificación. Sin esto solo imprime la lista (dry-run).",
    )

    return parser


def register_add_from_args(
    conn: Connection, user_id: int, args: argparse.Namespace, *, event_id: str | None = None
) -> dict[str, Any]:
    """Mapea `args` (ya parseados) → `register_card` sobre un `conn` DADO. Lo reusan `_cmd_add` (que
    abre su propia tx) y el cierre de evento del agente (tx compartida, que pasa su `event_id` para
    que la identidad correlacione vía `mismo_evento` con los otros hechos del evento)."""
    return register_card(
        conn,
        user_id,
        name=args.name,
        kind=args.kind,
        email=args.email,
        handle=args.handle,
        phone=args.phone,
        org=args.org,
        role=args.role,
        event_id=event_id,
    )


def _cmd_add(args: argparse.Namespace) -> int:
    """Resolve-or-create de una identidad desde una tarjeta de contacto. Delega en
    `register_card` (misma resolución que la extracción; sin LLM). Idempotente."""
    if args.org and args.kind != "persona":
        _say("--org solo aplica a --kind persona (la afiliación es persona↔org).", err=True)
        return 1
    with connection() as conn:
        row = register_add_from_args(conn, args.user, args)
    if args.as_json:
        _emit_json(row)
    else:
        verbo = "creada" if row["method"] == "created" else f"resuelta ({row['method']})"
        org = row.get("org")
        afil = f" · afiliada a #{org['id']} {org['display_name']!r}" if org else ""
        _say(f"\n{verbo}: id={row['id']} {row['display_name']!r} ({row['kind']}){afil}.\n")
    return 0


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
    """Upsert por nombre normalizado (no hay UNIQUE de negocio en mod_identidades): busca la org del
    user por `name_norm`; actualiza o inserta; los dominios van a identificadores."""
    with connection() as conn:
        # El lookup admite productos (una entidad ya reclasificada actualiza su interés en vez de
        # duplicarse como org); el INSERT de abajo sigue creando organizaciones.
        row = conn.execute(
            text(
                "SELECT id FROM mod_identidades "
                "WHERE user_id = :u AND kind IN ('organizacion','producto') "
                "AND name_norm = memex_norm(:n)"
            ),
            {"u": args.user, "n": args.name},
        ).first()
        if row is not None:
            org_id = int(row[0])
            conn.execute(
                text(
                    "UPDATE mod_identidades SET aliases = :aliases, interest = TRUE, "
                    "notes = :notes, updated_at = NOW() WHERE id = :id"
                ),
                {"aliases": list(args.alias), "notes": args.description, "id": org_id},
            )
        else:
            org_id = int(
                conn.execute(
                    text(
                        """
                        INSERT INTO mod_identidades
                          (user_id, kind, display_name, aliases, interest, source, notes)
                        VALUES (:u, 'organizacion', :n, :aliases, TRUE, 'manual', :notes)
                        RETURNING id
                        """
                    ),
                    {
                        "u": args.user,
                        "n": args.name,
                        "aliases": list(args.alias),
                        "notes": args.description,
                    },
                ).scalar_one()
            )
        for d in args.domain:
            if not d.strip():
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO mod_identidades_identifiers
                      (user_id, identity_id, platform, kind, value, value_norm, source)
                    VALUES (:u, :id, 'domain', 'domain', :v, :vn, 'manual')
                    ON CONFLICT (identity_id, platform, kind, value_norm) DO NOTHING
                    """
                ),
                {"u": args.user, "id": org_id, "v": d, "vn": norm_identifier("domain", d)},
            )
    _say(f"\nEn la lista de interés: id={org_id} {args.name!r}.\n")
    return 0


def _cmd_interest_list(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT mi.id, mi.display_name, mi.aliases,
                           (SELECT array_agg(value) FROM mod_identidades_identifiers
                            WHERE identity_id = mi.id AND kind = 'domain') AS domains
                    FROM mod_identidades mi
                    WHERE mi.user_id = :uid AND mi.kind IN ('organizacion','producto')
                      AND mi.interest
                    ORDER BY mi.display_name
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
        _say(f"  [{r['id']}] {r['display_name']} aliases=[{aliases}] domains=[{domains}]")
    _say("")
    return 0


def _cmd_interest_remove(args: argparse.Namespace) -> int:
    with connection() as conn:
        row = conn.execute(
            text(
                "DELETE FROM mod_identidades WHERE id = :id AND user_id = :uid "
                "RETURNING display_name"
            ),
            {"id": args.id, "uid": args.user},
        ).first()
    if row is None:
        _say(f"\nNo existe la identidad id={args.id} para el user {args.user}.\n", err=True)
        return 1
    _say(f"\nQuitada de la lista: {row[0]!r} (id={args.id}).\n")
    return 0


def _cmd_merge(args: argparse.Namespace) -> int:
    stats = asyncio.run(run_merge_phase2(args.user, limit=args.limit))
    _say(
        f"\nidentidades merge: pares={stats.pairs} fusionados={stats.merged} "
        f"rechazados={stats.rejected} errores={stats.errors}\n"
    )
    return 1 if stats.errors else 0


def _cmd_candidates(args: argparse.Namespace) -> int:
    with connection() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT c.id, c.score, a.display_name AS a_name, b.display_name AS b_name
                    FROM mod_identidades_merge_candidates c
                    JOIN mod_identidades a ON a.id = c.identity_a_id
                    JOIN mod_identidades b ON b.id = c.identity_b_id
                    WHERE c.user_id = :uid AND c.status = 'candidate'
                    ORDER BY c.score DESC NULLS LAST, c.id
                    """
                ),
                {"uid": args.user},
            )
            .mappings()
            .all()
        )
    if not rows:
        _say(f"\nSin candidatos de merge pendientes para el user {args.user}.\n")
        return 0
    _say(f"\nCandidatos de merge (user {args.user}):")
    for r in rows:
        score = f"{float(r['score']):.2f}" if r["score"] is not None else "?"
        _say(f"  [{r['id']}] {r['a_name']!r} ~ {r['b_name']!r} (score={score})")
    _say("")
    return 0


def _cmd_backfill_productos(args: argparse.Namespace) -> int:
    """Dry-run por default: imprime la lista de orgs que el voto reclasificaría y NO escribe.
    `--apply` aplica todo (kind + menciones + aristas + membresías + candidatos) en una tx."""
    with connection() as conn:
        cands = find_product_candidates(conn, args.user)
        if not cands:
            _say(f"\nSin candidatos org→producto para el user {args.user}.\n")
            return 0
        _say(f"\nCandidatos a reclasificar org→producto (user {args.user}):")
        for c in cands:
            _say(f"  [{c.id}] {c.display_name} votos={c.votos_producto}/{c.votos_total}")
        if not args.apply:
            _say(f"\nDRY-RUN: {len(cands)} candidatos; no se escribió nada. Aplicá con --apply.\n")
            return 0
        stats = apply_reclassification(conn, args.user, [c.id for c in cands])
    _say(
        f"\nReclasificadas {stats.reclassified} identidades a producto; "
        f"menciones={stats.mentions} aristas={stats.edges} "
        f"membresías={stats.cluster_members} "
        f"candidatos de merge rechazados={stats.merge_candidates_rejected}.\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.modules.identidades.cli")

    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "help":
        _say(_HELP)
        return 0
    log.info("identidades.cli.start", cmd=args.cmd)

    try:
        if args.cmd == "add":
            return _cmd_add(args)
        if args.cmd == "sync":
            return _cmd_sync(args)
        if args.cmd == "add-account":
            return _cmd_add_account(args)
        if args.cmd == "accounts":
            return _cmd_accounts(args)
        if args.cmd == "merge":
            return _cmd_merge(args)
        if args.cmd == "candidates":
            return _cmd_candidates(args)
        if args.cmd == "backfill-productos":
            return _cmd_backfill_productos(args)
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
