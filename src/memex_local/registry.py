"""Lógica de install/enable/disable/list/uninstall de plugins.

Coordina filesystem (`~/.memex-local/plugins/<name>/`) con SQLite local
(`state.plugins`). El filesystem es la fuente de verdad de QUÉ plugins
existen físicamente; la SQLite es la fuente de verdad de cuál está
habilitado y con qué schedule.

Las operaciones son idempotentes: instalar dos veces el mismo plugin no
es error; deshabilitar uno ya deshabilitado tampoco.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from memex_local.discovery import DiscoveryResult, discover_plugins
from memex_local.paths import plugins_dir
from memex_local.protocol import LocalPlugin
from memex_local.state import PluginRow, State


class RegistryError(Exception):
    """Operación sobre el registry inválida (plugin no existe, etc.)."""


@dataclass(frozen=True)
class PluginView:
    """Vista combinada del estado de un plugin: filesystem + SQLite."""

    name: str
    installed: bool
    enabled: bool
    version: str | None
    schedule: str | None
    source_id: int | None


def install_plugin(source_path: Path, dest_root: Path | None = None) -> str:
    """Copia un plugin desde `source_path` al directorio de plugins del cliente.

    El nombre del directorio destino se toma de `source_path.name`. Idempotente:
    si ya existe, sobrescribe. Devuelve el nombre del plugin instalado.
    """
    if not source_path.exists() or not source_path.is_dir():
        raise RegistryError(f"source path is not a directory: {source_path}")
    if not (source_path / "__init__.py").exists():
        raise RegistryError(f"source path missing __init__.py: {source_path}")

    dest_root = dest_root or plugins_dir()
    dest_root.mkdir(parents=True, exist_ok=True)
    dest = dest_root / source_path.name
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source_path, dest)
    return source_path.name


def uninstall_plugin(name: str, dest_root: Path | None = None, state: State | None = None) -> bool:
    """Borra el plugin del filesystem y del registry. Idempotente."""
    dest_root = dest_root or plugins_dir()
    dest = dest_root / name
    removed_fs = False
    if dest.exists():
        shutil.rmtree(dest)
        removed_fs = True
    removed_db = False
    if state is not None:
        removed_db = state.remove_plugin(name)
    return removed_fs or removed_db


def enable(name: str, state: State, plugins: dict[str, LocalPlugin]) -> None:
    if name not in plugins:
        raise RegistryError(f"plugin {name!r} not installed (or invalid)")
    plugin = plugins[name]
    state.upsert_plugin(
        name,
        version=plugin.version,
        schedule=plugin.default_schedule,
    )
    state.set_enabled(name, True)


def disable(name: str, state: State) -> None:
    if not state.set_enabled(name, False):
        raise RegistryError(f"plugin {name!r} not in registry")


def list_views(state: State, plugins_root: Path | None = None) -> list[PluginView]:
    """Combina filesystem (discovery) + registry (SQLite) en una sola vista."""
    plugins_root = plugins_root or plugins_dir()
    disc: DiscoveryResult = discover_plugins(plugins_root)
    discovered: set[str] = set(disc.plugins.keys())
    db_rows: dict[str, PluginRow] = {p.name: p for p in state.list_plugins()}

    names = discovered | set(db_rows.keys())
    views: list[PluginView] = []
    for name in sorted(names):
        plugin = disc.plugins.get(name)
        row = db_rows.get(name)
        views.append(
            PluginView(
                name=name,
                installed=plugin is not None,
                enabled=bool(row.enabled) if row else False,
                version=plugin.version if plugin else (row.version if row else None),
                schedule=(row.schedule if row else (plugin.default_schedule if plugin else None)),
                source_id=row.source_id if row else None,
            )
        )
    return views


def attach_source_id(state: State, name: str, source_id: int) -> None:
    """Persiste el source_id resuelto para un plugin (tras llamar ensure_source)."""
    state.upsert_plugin(name, source_id=source_id)
