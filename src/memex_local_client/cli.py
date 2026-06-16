"""CLI del cliente local: `memex-local-client <subcomando>`.

Subcomandos:

- `connect <url> [--token T]`          — conecta a memex: valida y escribe la config.
- `setup [--url --token --plugin]`     — onboarding guiado: conectar + instalar un plugin.
- `doctor`                             — diagnóstico: conexión + plugins listos.
- `autostart enable|disable|status`    — auto-arranque del daemon al iniciar sesión (Windows).
- `daemon start`                       — arranca el scheduler (bloquea).
- `plugin list`                        — qué plugins hay, instalados/habilitados.
- `plugin install <ruta>`              — copia un plugin al directorio del cliente.
- `plugin enable <nombre>`             — habilita en el registry.
- `plugin disable <nombre>`            — deshabilita.
- `plugin uninstall <nombre>`          — borra del filesystem y del registry.
- `plugin doctor <nombre>`             — chequea requisitos del plugin.
- `plugin authorize <nombre>`          — flujo OAuth interactivo (delega al plugin).
- `status`                             — resumen de últimas corridas por plugin.
- `runs [--plugin X] [--limit N]`      — historial detallado de corridas.

Lo primero es `connect` (o `setup`): sin eso no hay `config.toml` y el daemon no sabe a
qué servidor hablar. Auth setup separado: `plugin authorize` se invoca una vez por plugin
que use OAuth (típicamente el IMAP universitario).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from memex.logging import get_logger, setup_logging
from memex_local_client.config import LocalConfig, LocalConfigError
from memex_local_client.discovery import discover_plugins
from memex_local_client.paths import ensure_layout, main_config_path, plugins_dir
from memex_local_client.protocol import Problem
from memex_local_client.registry import (
    RegistryError,
    disable,
    enable,
    install_plugin,
    list_views,
    uninstall_plugin,
)
from memex_local_client.run import load_plugin_config
from memex_local_client.scheduler import Scheduler
from memex_local_client.state import open_state


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-local-client")
    sub = p.add_subparsers(dest="group", required=True)

    connect_p = sub.add_parser("connect", help="Conecta a memex (valida + escribe la config).")
    connect_p.add_argument("url", help="URL del gateway/API, p.ej. http://localhost:8787")
    connect_p.add_argument(
        "--token", default="", help="Bearer token (si el server tiene auth enforced)."
    )

    setup_p = sub.add_parser("setup", help="Onboarding guiado: conectar + instalar un plugin.")
    setup_p.add_argument("--url", default=None, help="URL del gateway (si falta, se pregunta).")
    setup_p.add_argument("--token", default="", help="Bearer token opcional.")
    setup_p.add_argument(
        "--plugin", default=None, help="plugin bundled a instalar (selftest|outlook-desktop|…)."
    )
    setup_p.add_argument(
        "--from",
        dest="from_dir",
        default=None,
        help="dir de plugins bundled (default: <repo>/plugins).",
    )

    sub.add_parser("doctor", help="Diagnóstico: conexión + plugins listos.")

    backfill_p = sub.add_parser(
        "backfill", help="Trae una ventana histórica de un plugin (no toca el incremental)."
    )
    backfill_p.add_argument(
        "plugin", help="Nombre del plugin (outlook-desktop, imap-university, …)."
    )
    when = backfill_p.add_mutually_exclusive_group(required=True)
    when.add_argument("--months", type=int, help="Últimos N meses.")
    when.add_argument("--days", type=int, help="Últimos N días.")
    when.add_argument("--since", help="Fecha de inicio YYYY-MM-DD.")
    backfill_p.add_argument(
        "--until", default=None, help="Fecha de fin YYYY-MM-DD (default: ahora)."
    )
    backfill_p.add_argument(
        "--dry-run", action="store_true", help="Cuenta sin ingerir (previsualiza el volumen)."
    )

    autostart_p = sub.add_parser("autostart", help="Auto-arranque del daemon (Windows).")
    asub = autostart_p.add_subparsers(dest="cmd", required=True)
    asub.add_parser("enable", help="Registra la tarea (corre al iniciar sesión).")
    asub.add_parser("disable", help="Quita la tarea.")
    asub.add_parser("status", help="¿está registrada?")

    daemon = sub.add_parser("daemon", help="Daemon lifecycle.")
    dsub = daemon.add_subparsers(dest="cmd", required=True)
    dsub.add_parser("start", help="Arranca el scheduler (bloquea).")

    plugin = sub.add_parser("plugin", help="Gestión de plugins.")
    psub = plugin.add_subparsers(dest="cmd", required=True)
    psub.add_parser("list", help="Lista plugins instalados y su estado.")
    install_p = psub.add_parser("install", help="Copia un plugin al directorio del cliente.")
    install_p.add_argument("path", help="Ruta a un directorio que contiene __init__.py")
    enable_p = psub.add_parser("enable")
    enable_p.add_argument("name")
    disable_p = psub.add_parser("disable")
    disable_p.add_argument("name")
    uninst_p = psub.add_parser("uninstall")
    uninst_p.add_argument("name")
    doctor_p = psub.add_parser("doctor", help="Chequea requisitos de un plugin.")
    doctor_p.add_argument("name")
    auth_p = psub.add_parser("authorize", help="Flujo interactivo (OAuth) del plugin.")
    auth_p.add_argument("name")

    sub.add_parser("status", help="Resumen de últimas corridas.")

    runs_p = sub.add_parser("runs", help="Historial detallado.")
    runs_p.add_argument("--plugin", default=None)
    runs_p.add_argument("--limit", type=int, default=20)

    return p


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    setup_logging()
    log = get_logger("memex_local_client.cli")
    ensure_layout()

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.group == "connect":
            return _cmd_connect(args, log)
        if args.group == "setup":
            return _cmd_setup(args, log)
        if args.group == "doctor":
            return _cmd_doctor_top(log)
        if args.group == "backfill":
            return _cmd_backfill(args, log)
        if args.group == "autostart":
            return _cmd_autostart(args, log)
        if args.group == "daemon" and args.cmd == "start":
            return _cmd_daemon_start(log)
        if args.group == "plugin":
            return _cmd_plugin(args, log)
        if args.group == "status":
            return _cmd_status()
        if args.group == "runs":
            return _cmd_runs(args)
    except LocalConfigError as e:
        log.error("memex_local_client.cli.config_error", reason=str(e))
        return 1
    except RegistryError as e:
        log.error("memex_local_client.cli.registry_error", reason=str(e))
        return 1
    except Exception as e:
        log.exception("memex_local_client.cli.fatal", exc=str(e))
        return 1

    parser.print_help()
    return 1


def _prompt(label: str, default: str = "") -> str:
    """Pide un valor por stdin si hay TTY; si no, devuelve el default (testeable/automatizable)."""
    if not sys.stdin.isatty():
        return default
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return ans or default


def _cmd_connect(args: argparse.Namespace, log: Any) -> int:
    from memex_local_client.connect import ConnectError, connect

    try:
        who = connect(args.url, args.token)
    except ConnectError as e:
        print(f"[x] no se pudo conectar: {e}")
        return 1
    extra = " (con token)" if args.token else ""
    print(
        f"[ok] conectado a {args.url.rstrip('/')} como {who.email or 'usuario'} "
        f"(user_id={who.user_id}){extra}"
    )
    print(f"     config: {main_config_path()}")
    if who.auth_enforced and not args.token:
        print("     nota: el server pide auth; reconectá con --token si la ingesta falla.")
    print("     siguiente: 'setup' para instalar un plugin, o 'daemon start' si ya tenés uno.")
    return 0


def _cmd_setup(args: argparse.Namespace, log: Any) -> int:
    from memex_local_client.connect import ConnectError, bundled_plugins_dir, connect

    url = args.url or _prompt("URL del gateway", "http://localhost:8787")
    try:
        who = connect(url, args.token)
    except ConnectError as e:
        print(f"[x] no se pudo conectar: {e}")
        return 1
    print(f"[ok] conectado como {who.email or 'usuario'} (user_id={who.user_id})")

    bundled = Path(args.from_dir) if args.from_dir else bundled_plugins_dir()
    if bundled is None or not bundled.exists():
        print("[x] no encuentro los plugins bundled; pasá --from <dir>.")
        return 1
    available = sorted(
        d.name for d in bundled.iterdir() if d.is_dir() and (d / "__init__.py").exists()
    )
    print("plugins disponibles: " + (", ".join(available) or "(ninguno)"))

    plugin = args.plugin or _prompt("¿cuál instalar? (enter = ninguno)", "")
    if not plugin:
        print("listo. instalá uno cuando quieras: plugin install <ruta>")
        return 0
    if plugin not in available:
        print(f"[x] {plugin!r} no está en {bundled}")
        return 1

    name = install_plugin(bundled / plugin)
    disc = discover_plugins(plugins_dir())
    with open_state() as state:
        enable(name, state, disc.plugins)
    log.info("memex_local_client.cli.plugin_installed", name=name)

    example = plugins_dir() / name / "config.example.toml"
    dst = plugins_dir() / name / "config.toml"
    if example.exists() and not dst.exists():
        dst.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[ok] copié config.example.toml -> config.toml ({dst})")
        print("     revisá/completá ese archivo antes de correr el daemon.")
    print(f"[ok] {name} instalado y habilitado.")
    for prob in disc.plugins[name].validate_requirements(load_plugin_config(name, plugins_dir())):
        print(f"     [{prob.severity}] {prob.code}: {prob.message}")
    print("siguiente: 'daemon start' (o 'autostart enable' en Windows).")
    return 0


def _cmd_doctor_top(log: Any) -> int:
    from memex_local_client.connect import ConnectError, check_connection

    try:
        cfg = LocalConfig.load()
    except LocalConfigError as e:
        print(f"[x] sin conexión configurada: {e}")
        print("    corré: memex-local-client connect http://localhost:8787")
        return 1
    print(f"config: {main_config_path()}")
    print(f"  gateway_url = {cfg.gateway_url}")
    print(f"  api_token   = {'(set)' if cfg.api_token else '(vacio)'}")
    try:
        who = check_connection(cfg.gateway_url, cfg.api_token)
    except ConnectError as e:
        print(f"conexion: [x] {e}")
        return 1
    print(
        f"conexion: [ok] {who.email or 'usuario'} "
        f"(user_id={who.user_id}, auth_enforced={who.auth_enforced})"
    )

    disc = discover_plugins(plugins_dir())
    with open_state() as state:
        views = list_views(state)
    if not views:
        print("plugins: ninguno instalado. usá 'setup' o 'plugin install <ruta>'.")
    else:
        print("plugins:")
        for v in views:
            tag = "ENABLED" if v.enabled else ("installed" if v.installed else "registered")
            print(f"  {v.name:24s} {tag:11s} schedule={v.schedule} source_id={v.source_id}")
            if v.enabled and v.name in disc.plugins:
                probs = disc.plugins[v.name].validate_requirements(
                    load_plugin_config(v.name, plugins_dir())
                )
                for prob in probs:
                    print(f"      [{prob.severity}] {prob.code}: {prob.message}")
    for err in disc.errors:
        print(f"  ! {err.plugin_dir.name}: {err.reason}")
    return 0


def _cmd_backfill(args: argparse.Namespace, log: Any) -> int:
    from memex.ingestors.runner import RunStats
    from memex_local_client.backfill import BackfillError, resolve_window, run_backfill

    disc = discover_plugins(plugins_dir())
    if args.plugin not in disc.plugins:
        print(f"[x] plugin {args.plugin!r} no instalado. Instalados: {sorted(disc.plugins)}")
        return 1
    plugin = disc.plugins[args.plugin]
    try:
        window = resolve_window(
            months=args.months, days=args.days, since=args.since, until=args.until
        )
    except BackfillError as e:
        print(f"[x] {e}")
        return 1

    cfg = LocalConfig.load()
    tag = " (dry-run)" if args.dry_run else ""
    print(f"backfill {args.plugin}: {window.since.date()} -> {window.until.date()}{tag}")

    def progress(stats: RunStats) -> None:
        verb = "escaneados" if args.dry_run else "posteados"
        print(f"  ...{verb} {stats.posted} (nuevos {stats.inserted}, dup {stats.duplicates})")

    with open_state() as state:
        try:
            stats = run_backfill(
                plugin,
                gateway_url=cfg.gateway_url,
                api_token=cfg.api_token or None,
                plugins_root=plugins_dir(),
                window=window,
                state=state,
                dry_run=args.dry_run,
                on_chunk=progress,
            )
        except Exception as e:
            log.exception("memex_local_client.cli.backfill_failed", plugin=args.plugin, exc=str(e))
            print(f"[x] backfill falló: {e}")
            return 1

    if args.dry_run:
        print(f"[ok] dry-run: {stats.posted} correos en la ventana (no se ingirió nada).")
    else:
        print(
            f"[ok] backfill: {stats.inserted} nuevos, {stats.duplicates} ya estaban, "
            f"{stats.filtered} filtrados ({stats.posted} escaneados)."
        )
    return 0


def _cmd_autostart(args: argparse.Namespace, log: Any) -> int:
    from memex_local_client import autostart

    try:
        if args.cmd == "enable":
            res = autostart.enable()
        elif args.cmd == "disable":
            res = autostart.disable()
        else:
            res = autostart.status()
    except autostart.AutostartError as e:
        print(f"[x] {e}")
        return 1
    print(("[ok] " if res.ok else "[--] ") + res.message)
    return 0


def _cmd_daemon_start(log: Any) -> int:
    cfg = LocalConfig.load()
    log.info("memex_local_client.daemon.starting", gateway_url=cfg.gateway_url)
    state = open_state()
    sched = Scheduler(
        state=state,
        gateway_url=cfg.gateway_url,
        api_token=cfg.api_token or None,
        plugins_root=plugins_dir(),
    )
    sched.install_signal_handlers()
    try:
        sched.run_forever()
    finally:
        state.close()
    return 0


def _cmd_plugin(args: argparse.Namespace, log: Any) -> int:
    if args.cmd == "list":
        with open_state() as state:
            for v in list_views(state):
                tag = (
                    "ENABLED" if v.enabled else ("installed" if v.installed else "registered-only")
                )
                print(f"  {v.name:30s}  {tag:18s}  schedule={v.schedule}  source_id={v.source_id}")
        return 0

    if args.cmd == "install":
        name = install_plugin(Path(args.path))
        log.info("memex_local_client.cli.plugin_installed", name=name)
        print(f"plugin {name!r} instalado en {plugins_dir() / name}")
        return 0

    if args.cmd == "enable":
        disc = discover_plugins(plugins_dir())
        with open_state() as state:
            enable(args.name, state, disc.plugins)
        log.info("memex_local_client.cli.plugin_enabled", name=args.name)
        return 0

    if args.cmd == "disable":
        with open_state() as state:
            disable(args.name, state)
        log.info("memex_local_client.cli.plugin_disabled", name=args.name)
        return 0

    if args.cmd == "uninstall":
        with open_state() as state:
            removed = uninstall_plugin(args.name, state=state)
        if not removed:
            print(f"plugin {args.name!r} no encontrado.")
            return 1
        log.info("memex_local_client.cli.plugin_uninstalled", name=args.name)
        return 0

    if args.cmd == "doctor":
        return _cmd_plugin_doctor(args.name)

    if args.cmd == "authorize":
        return _cmd_plugin_authorize(args.name, log)

    return 1


def _cmd_plugin_doctor(name: str) -> int:
    disc = discover_plugins(plugins_dir())
    if name not in disc.plugins:
        print(f"plugin {name!r} no instalado o inválido.")
        for err in disc.errors:
            if err.plugin_dir.name == name:
                print(f"  motivo: {err.reason}")
        return 1
    plugin = disc.plugins[name]
    cfg = load_plugin_config(name, plugins_dir())
    problems: list[Problem] = plugin.validate_requirements(cfg)
    if not problems:
        print(f"plugin {name!r}: requisitos OK.")
        return 0
    errors = sum(1 for p in problems if p.severity == "error")
    for prob in problems:
        print(f"  [{prob.severity}] {prob.code}: {prob.message}")
    return 1 if errors else 0


def _cmd_plugin_authorize(name: str, log: Any) -> int:
    """Delega al método `authorize_interactive` del plugin si lo tiene."""
    disc = discover_plugins(plugins_dir())
    if name not in disc.plugins:
        print(f"plugin {name!r} no instalado o inválido.")
        return 1
    plugin = disc.plugins[name]
    cfg = load_plugin_config(name, plugins_dir())
    authorize = getattr(plugin, "authorize_interactive", None)
    if authorize is None:
        print(f"plugin {name!r} no implementa authorize_interactive.")
        return 1
    try:
        authorize(cfg)
    except Exception as e:
        log.exception("memex_local_client.cli.authorize_failed", plugin=name, exc=str(e))
        return 1
    print(f"autorización completada para {name!r}.")
    return 0


def _cmd_status() -> int:
    with open_state() as state:
        for v in list_views(state):
            recent = state.recent_runs(v.name, limit=1)
            last = recent[0] if recent else None
            last_str = (
                f"last={last.status}@{last.finished_at or last.started_at} "
                f"ins={last.inserted} dup={last.duplicates} err={last.errors} flt={last.filtered}"
                if last
                else "no runs yet"
            )
            tag = "ENABLED" if v.enabled else "disabled"
            print(f"  {v.name:30s} {tag:10s} {last_str}")
    return 0


def _cmd_runs(args: argparse.Namespace) -> int:
    with open_state() as state:
        rows = state.recent_runs(args.plugin, limit=args.limit)
        for r in rows:
            print(
                f"  #{r.id:5d} {r.plugin_name:25s} {r.status:8s} {r.mode:11s} "
                f"started={r.started_at} finished={r.finished_at or '-'} "
                f"ins={r.inserted} dup={r.duplicates} err={r.errors} flt={r.filtered}"
                + (f" reason={r.error_msg}" if r.error_msg else "")
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
