"""CLI `memex-notifications` — el daemon de notificaciones y su configuración.

  daemon start            arranca el loop (bloquea). Apagado por default: `enable` lo arma.
  enable | disable        prende/apaga el daemon en la DB (lo relee cada tick).
  status                  ajustes, productores (activo/redacción/tiempos) y canales.
  run [--dry-run] [--producer N]
                          UN ciclo (producir + entregar) y sale. --dry-run no escribe ni llama LLM.
  producers show|set      `set <nombre> [--enable|--disable] [--render plantilla|llm]
                          [--set clave=valor ...]` (p. ej. `--set hora=07:30`).
  channels list|add|rm|enable|disable|test
                          `add telegram --target <chat_id> [--personal info] [--sistema critica]
                          [--quiet 22:00-07:00]`; `test <id>` manda un mensaje de prueba.
  deliveries [--limit]    últimas entregas.
  settings --timezone     zona horaria del usuario (horas de los productores y del silencio).
  purge                   borra los avisos vencidos (housekeeping).

Server-side: usa la DB de memex del .env (`doppler run -- memex-notifications …`).
Exit 0 si OK; 1 error; 2 argumentos inválidos.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv

from memex.db import connection
from memex.logging import get_logger, setup_logging
from memex.notifications import store
from memex.notifications.channels import ChannelError, build_channel, channel_names
from memex.notifications.client import SEVERITIES, Notification
from memex.notifications.daemon import NotificationsDaemon
from memex.notifications.producers import all_producers
from memex.notifications.service import run_cycle
from memex.notifications.settings import (
    RENDERS,
    NotificationsEnv,
    coerce_config,
    get_producer_settings,
    get_settings,
    parse_hhmm,
    upsert_producer_settings,
    upsert_settings,
)


def _say(msg: str, *, err: bool = False) -> None:
    enc = sys.stdout.encoding or "utf-8"
    safe = msg.encode(enc, errors="replace").decode(enc, errors="replace")
    print(safe, file=sys.stderr if err else sys.stdout)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="memex-notifications")
    parser.add_argument("--user", type=int, default=None, help="User id (default: env o 1).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("purge", help="Borra los avisos vencidos (housekeeping).")

    daemon = sub.add_parser("daemon", help="Lifecycle del daemon.")
    dsub = daemon.add_subparsers(dest="dcmd", required=True)
    dsub.add_parser("start", help="Arranca el daemon (bloquea). Apagado por default.")

    sub.add_parser("enable", help="Prende el daemon (DB).")
    sub.add_parser("disable", help="Apaga el daemon (DB).")
    sub.add_parser("status", help="Ajustes, productores y canales.")

    run = sub.add_parser("run", help="Un ciclo: producir + entregar.")
    run.add_argument("--dry-run", action="store_true", help="No escribe ni llama al LLM.")
    run.add_argument("--producer", default=None, help="Solo este productor.")
    run.add_argument("--json", action="store_true")

    prod = sub.add_parser("producers", help="Productores de avisos.")
    psub = prod.add_subparsers(dest="pcmd", required=True)
    psub.add_parser("show", help="Lista productores con su config efectiva.")
    pset = psub.add_parser("set", help="Cambia un productor.")
    pset.add_argument("name")
    g = pset.add_mutually_exclusive_group()
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    pset.add_argument("--render", choices=RENDERS, default=None)
    pset.add_argument("--set", dest="changes", action="append", default=[], metavar="CLAVE=VALOR")

    ch = sub.add_parser("channels", help="Canales de entrega.")
    csub = ch.add_subparsers(dest="ccmd", required=True)
    csub.add_parser("list")
    cadd = csub.add_parser("add", help="Alta de un canal (o reemplazo del mismo canal+destino).")
    cadd.add_argument("channel", choices=channel_names())
    cadd.add_argument("--target", default="", help="telegram: chat_id.")
    cadd.add_argument(
        "--personal",
        default="info",
        help="Severidad mínima de lo personal (info|alta|critica|no). Default info.",
    )
    cadd.add_argument(
        "--sistema",
        default="critica",
        help="Severidad mínima de lo interno de memex (info|alta|critica|no). Default critica.",
    )
    cadd.add_argument("--quiet", default=None, help="Silencio HH:MM-HH:MM (solo pasa lo crítico).")
    for name in ("rm", "enable", "disable", "test"):
        p = csub.add_parser(name)
        p.add_argument("id", type=int)
        if name == "test":
            p.add_argument("--text", default="Prueba de notificaciones de memex.")

    deliv = sub.add_parser("deliveries", help="Últimas entregas.")
    deliv.add_argument("--limit", type=int, default=30)

    st = sub.add_parser("settings", help="Ajustes generales.")
    st.add_argument("--timezone", default=None)
    return parser


def _subs(personal: str, sistema: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for audience, value in (("personal", personal), ("sistema", sistema)):
        v = value.strip().lower()
        if v in ("no", "off", "none", ""):
            continue
        if v not in SEVERITIES:
            raise ValueError(f"severidad inválida para {audience}: {value!r}; {SEVERITIES} o no")
        out[audience] = v
    return out


def _cmd_status(user_id: int, env: NotificationsEnv) -> int:
    with connection() as conn:
        s = get_settings(conn, user_id, default_tz=env.timezone)
        _say(f"daemon: {'prendido' if s.daemon_enabled else 'apagado'} · zona {s.timezone}")
        _say("productores:")
        _print_producers(conn, user_id)
        _say("canales:")
        chans = store.list_channels(conn, user_id=user_id)
    if not chans:
        _say("  (ninguno; `channels add telegram --target <chat_id>`)")
    for c in chans:
        _say("  " + _fmt_channel(c))
    return 0


def _print_producers(conn: Any, user_id: int) -> None:
    for spec in all_producers().values():
        ps = get_producer_settings(
            conn,
            user_id,
            spec.name,
            default_enabled=spec.default_enabled,
            default_render=spec.default_render,
            default_config=spec.default_config,
        )
        state = "activo" if ps.enabled else "apagado"
        render = ps.render if spec.llm_allowed else "plantilla"
        cfg = " ".join(f"{k}={v}" for k, v in ps.config.items())
        origin = "" if ps.from_db else " (defaults)"
        _say(f"  {spec.name:<20} {state:<8} {render:<9} {cfg}{origin}")
        _say(f"  {'':<20} {spec.audience}: {spec.description}")


def _fmt_channel(c: dict[str, Any]) -> str:
    subs = ", ".join(f"{k}≥{v}" for k, v in dict(c["subscriptions"]).items()) or "sin suscripciones"
    quiet = (
        f" · silencio {c['quiet_from']:%H:%M}-{c['quiet_until']:%H:%M}"
        if c["quiet_from"] and c["quiet_until"]
        else ""
    )
    state = "activo" if c["enabled"] else "apagado"
    return f"#{c['id']} {c['channel']} → {c['target'] or '-'} · {state} · {subs}{quiet}"


def _cmd_producers(args: argparse.Namespace, user_id: int) -> int:
    with connection() as conn:
        if args.pcmd == "show":
            _print_producers(conn, user_id)
            return 0
        spec = all_producers().get(args.name)
        if spec is None:
            _say(
                f"productor desconocido: {args.name}; válidos: {sorted(all_producers())}",
                err=True,
            )
            return 2
        if args.render == "llm" and not spec.llm_allowed:
            _say(f"{spec.name} es solo plantilla (no hay nada que redactar).", err=True)
            return 2
        changes: dict[str, str] = {}
        for raw in args.changes:
            if "=" not in raw:
                _say(f"--set espera clave=valor, no {raw!r}", err=True)
                return 2
            k, v = raw.split("=", 1)
            changes[k.strip()] = v
        try:
            cfg = coerce_config(spec.default_config, changes)
            ps = upsert_producer_settings(
                conn,
                user_id,
                spec.name,
                default_enabled=spec.default_enabled,
                default_render=spec.default_render,
                default_config=spec.default_config,
                enabled=True if args.enable else (False if args.disable else None),
                render=args.render,
                config_changes=cfg,
            )
        except ValueError as e:
            _say(str(e), err=True)
            return 2
    _say(
        f"{spec.name}: {'activo' if ps.enabled else 'apagado'} · {ps.render} · "
        + " ".join(f"{k}={v}" for k, v in ps.config.items())
    )
    return 0


def _cmd_channels(args: argparse.Namespace, user_id: int, env: NotificationsEnv) -> int:
    if args.ccmd == "list":
        with connection() as conn:
            chans = store.list_channels(conn, user_id=user_id)
        for c in chans:
            _say(_fmt_channel(c))
        if not chans:
            _say("(sin canales)")
        return 0
    if args.ccmd == "add":
        try:
            subs = _subs(args.personal, args.sistema)
            quiet: tuple[Any, Any] = (None, None)
            if args.quiet:
                a, b = args.quiet.split("-", 1)
                quiet = (parse_hhmm(a), parse_hhmm(b))
        except ValueError as e:
            _say(str(e), err=True)
            return 2
        if args.channel == "telegram" and not args.target.strip().lstrip("-").isdigit():
            _say("telegram necesita --target <chat_id numérico>.", err=True)
            return 2
        with connection() as conn:
            cid = store.upsert_channel(
                conn,
                user_id=user_id,
                channel=args.channel,
                target=args.target.strip(),
                subscriptions=subs,
                quiet_from=quiet[0],
                quiet_until=quiet[1],
            )
            created = store.get_channel(conn, user_id=user_id, channel_id=cid)
        _say(_fmt_channel(created) if created else f"canal #{cid}")
        return 0
    if args.ccmd == "test":
        with connection() as conn:
            row = store.get_channel(conn, user_id=user_id, channel_id=args.id)
        if row is None:
            _say("canal no encontrado", err=True)
            return 1
        try:
            channel = build_channel(str(row["channel"]), env)
        except ChannelError as e:
            _say(str(e), err=True)
            return 1
        n = Notification(
            kind="prueba",
            severity="info",
            title="memex",
            body=args.text,
            dedup_key="",
            created_at=datetime.now(UTC),
            user_id=user_id,
        )

        async def _send() -> str | None:
            try:
                return await channel.send(str(row["target"]), f"memex\n{args.text}", n)
            finally:
                await channel.aclose()

        try:
            ref = asyncio.run(_send())
        except ChannelError as e:
            _say(f"falló: {e}", err=True)
            return 1
        _say(f"enviado por {row['channel']} a {row['target']}" + (f" (ref {ref})" if ref else ""))
        return 0
    with connection() as conn:
        if args.ccmd == "rm":
            ok = store.remove_channel(conn, user_id=user_id, channel_id=args.id)
        else:
            ok = store.update_channel(
                conn, user_id=user_id, channel_id=args.id, enabled=args.ccmd == "enable"
            )
    if not ok:
        _say("canal no encontrado", err=True)
        return 1
    _say("ok")
    return 0


def _cmd_run(args: argparse.Namespace, user_id: int, env: NotificationsEnv) -> int:
    if args.producer and args.producer not in all_producers():
        _say(f"productor desconocido: {args.producer}", err=True)
        return 2
    stats = asyncio.run(
        run_cycle(user_id, env=env, only_producer=args.producer, dry_run=args.dry_run)
    )
    if args.json:
        _say(json.dumps(stats.__dict__, ensure_ascii=False, default=str))
        return 0
    _say(
        f"productores={stats.producers} borradores={stats.drafts} encolados={stats.enqueued} "
        f"ya_existian={stats.skipped_existing} llm={stats.rendered_llm} pendientes={stats.pending} "
        f"entregados={stats.delivered} fallidos={stats.failed} silencio={stats.held_quiet} "
        f"bajo_severidad={stats.below_severity}"
    )
    for p in stats.preview:
        if "deliver" in p:
            _say(f"  [entregaría] {p['deliver']}: {p['title']}")
        else:
            body = p["body"].replace("\n", "\n    ")
            _say(f"  [{p['producer']} · {p['render']}] {p['title']}\n    {body}")
    for e in stats.errors:
        _say(f"  error: {e}", err=True)
    return 1 if stats.errors and not stats.did_something else 0


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex.notifications.cli")
    args = _build_parser().parse_args(argv)
    env = NotificationsEnv()
    user_id = args.user if args.user is not None else env.user_id
    log.info("notifications.cli.start", cmd=args.cmd, user_id=user_id)
    try:
        if args.cmd == "purge":
            with connection() as conn:
                deleted = store.purge_expired(conn)
            _say(f"Purgados {deleted} avisos vencidos.")
            return 0
        if args.cmd == "daemon" and args.dcmd == "start":
            daemon = NotificationsDaemon(env)

            async def _serve() -> None:
                daemon.install_signal_handlers()
                await daemon.run_forever()

            asyncio.run(_serve())
            return 0
        if args.cmd in ("enable", "disable"):
            with connection() as conn:
                s = upsert_settings(conn, user_id, daemon_enabled=args.cmd == "enable")
            _say(f"daemon: {'prendido' if s.daemon_enabled else 'apagado'}")
            return 0
        if args.cmd == "settings":
            with connection() as conn:
                s = upsert_settings(conn, user_id, timezone=args.timezone)
            _say(f"zona horaria: {s.timezone}")
            return 0
        if args.cmd == "status":
            return _cmd_status(user_id, env)
        if args.cmd == "run":
            return _cmd_run(args, user_id, env)
        if args.cmd == "producers":
            return _cmd_producers(args, user_id)
        if args.cmd == "channels":
            return _cmd_channels(args, user_id, env)
        if args.cmd == "deliveries":
            with connection() as conn:
                rows = store.list_deliveries(conn, user_id=user_id, limit=args.limit)
            for r in rows:
                when = r["sent_at"] or r["updated_at"]
                extra = f" · {r['error']}" if r["error"] else ""
                _say(
                    f"#{r['id']} {when:%Y-%m-%d %H:%M} {r['channel']}→{r['target']} "
                    f"{r['status']} x{r['attempts']} · [{r['audience']}/{r['severity']}] "
                    f"{r['title']}{extra}"
                )
            if not rows:
                _say("(sin entregas)")
            return 0
    except Exception as e:
        log.exception("notifications.cli.fatal", exc=str(e))
        _say(f"error: {e}", err=True)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
