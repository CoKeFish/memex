"""CLI del cliente local: `memex-local <subcomando>`.

Subcomandos:

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

Auth setup separado: el comando `memex-local plugin authorize` se invoca una
vez por plugin que use OAuth (típicamente el IMAP universitario).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from memex.logging import get_logger, setup_logging
from memex_local.config import LocalConfig, LocalConfigError
from memex_local.discovery import discover_plugins
from memex_local.paths import ensure_layout, plugins_dir
from memex_local.protocol import Problem
from memex_local.registry import (
    RegistryError,
    disable,
    enable,
    install_plugin,
    list_views,
    uninstall_plugin,
)
from memex_local.run import load_plugin_config
from memex_local.scheduler import Scheduler
from memex_local.state import open_state


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memex-local")
    sub = p.add_subparsers(dest="group", required=True)

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
    log = get_logger("memex_local.cli")
    ensure_layout()

    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        if args.group == "daemon" and args.cmd == "start":
            return _cmd_daemon_start(log)
        if args.group == "plugin":
            return _cmd_plugin(args, log)
        if args.group == "status":
            return _cmd_status()
        if args.group == "runs":
            return _cmd_runs(args)
    except LocalConfigError as e:
        log.error("memex_local.cli.config_error", reason=str(e))
        return 1
    except RegistryError as e:
        log.error("memex_local.cli.registry_error", reason=str(e))
        return 1
    except Exception as e:
        log.exception("memex_local.cli.fatal", exc=str(e))
        return 1

    parser.print_help()
    return 1


def _cmd_daemon_start(log: Any) -> int:
    cfg = LocalConfig.load()
    log.info("memex_local.daemon.starting", bridge_url=cfg.bridge_url)
    state = open_state()
    sched = Scheduler(
        state=state,
        bridge_url=cfg.bridge_url,
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
        log.info("memex_local.cli.plugin_installed", name=name)
        print(f"plugin {name!r} instalado en {plugins_dir() / name}")
        return 0

    if args.cmd == "enable":
        disc = discover_plugins(plugins_dir())
        with open_state() as state:
            enable(args.name, state, disc.plugins)
        log.info("memex_local.cli.plugin_enabled", name=args.name)
        return 0

    if args.cmd == "disable":
        with open_state() as state:
            disable(args.name, state)
        log.info("memex_local.cli.plugin_disabled", name=args.name)
        return 0

    if args.cmd == "uninstall":
        with open_state() as state:
            removed = uninstall_plugin(args.name, state=state)
        if not removed:
            print(f"plugin {args.name!r} no encontrado.")
            return 1
        log.info("memex_local.cli.plugin_uninstalled", name=args.name)
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
        log.exception("memex_local.cli.authorize_failed", plugin=name, exc=str(e))
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
                f"ins={last.inserted} dup={last.duplicates} err={last.errors}"
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
                f"  #{r.id:5d} {r.plugin_name:25s} {r.status:8s} "
                f"started={r.started_at} finished={r.finished_at or '-'} "
                f"ins={r.inserted} dup={r.duplicates} err={r.errors}"
                + (f" reason={r.error_msg}" if r.error_msg else "")
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
