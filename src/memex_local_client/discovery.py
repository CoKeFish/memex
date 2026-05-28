"""Carga dinámica de plugins desde el filesystem.

El usuario dropea un plugin (paquete Python con `__init__.py` que cumple
`LocalPlugin`) en `~/.memex-local-client/plugins/<nombre>/`. Al arrancar el daemon
(o vía `plugin list`), se escanea ese directorio y se intenta importar cada
subdirectorio como módulo.

Un plugin se considera **válido** si:
- Tiene un `__init__.py`.
- Importa sin lanzar excepción.
- Expone un objeto módulo o un atributo `plugin` que cumple el Protocol
  `LocalPlugin` (atributos: name, version, source_type, default_schedule;
  métodos: build_source, validate_requirements).
- Su `name` coincide con el del directorio.

Plugins inválidos se reportan con detalle pero no tumban el discovery —
podés tener plugins rotos en el directorio sin que eso impida levantar los
que sí funcionan.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memex_local_client.protocol import LocalPlugin


@dataclass(frozen=True)
class DiscoveryError:
    plugin_dir: Path
    reason: str


@dataclass(frozen=True)
class DiscoveryResult:
    plugins: dict[str, LocalPlugin]
    errors: list[DiscoveryError]


def discover_plugins(plugins_root: Path) -> DiscoveryResult:
    """Escanea `plugins_root` y devuelve los plugins válidos + errores."""
    plugins: dict[str, LocalPlugin] = {}
    errors: list[DiscoveryError] = []

    if not plugins_root.exists():
        return DiscoveryResult(plugins=plugins, errors=errors)

    for child in sorted(plugins_root.iterdir()):
        if not child.is_dir():
            continue
        init_py = child / "__init__.py"
        if not init_py.exists():
            errors.append(DiscoveryError(child, "missing __init__.py"))
            continue
        try:
            obj = _load_plugin(child)
        except Exception as e:
            errors.append(DiscoveryError(child, f"import failed: {type(e).__name__}: {e}"))
            continue

        if not isinstance(obj, LocalPlugin):
            errors.append(
                DiscoveryError(
                    child,
                    "module does not implement LocalPlugin protocol "
                    "(missing one of: name, version, source_type, "
                    "default_schedule, build_source, validate_requirements)",
                )
            )
            continue

        if obj.name != child.name:
            errors.append(
                DiscoveryError(
                    child,
                    f"plugin.name={obj.name!r} does not match directory name {child.name!r}",
                )
            )
            continue

        plugins[obj.name] = obj

    return DiscoveryResult(plugins=plugins, errors=errors)


def _load_plugin(plugin_dir: Path) -> Any:
    """Importa el plugin como un módulo aislado, sin contaminar sys.path globalmente.

    Si el módulo expone un atributo `plugin`, lo retorna; si no, retorna el
    módulo entero (un módulo puede satisfacer el Protocol estructuralmente
    si sus top-level vars + funciones cumplen la firma).
    """
    mod_name = f"memex_local_client._plugins.{plugin_dir.name}"
    spec = importlib.util.spec_from_file_location(
        mod_name, plugin_dir / "__init__.py", submodule_search_locations=[str(plugin_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not create spec for {plugin_dir}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return getattr(module, "plugin", module)
