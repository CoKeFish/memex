"""Guard de CI: cada plugin bundled construye un Source que cumple el contrato.

El runner accede a `checkpoint_schema`/`fetch`/`advance_checkpoint`/etc. por atributo;
un plugin cuyo `build_source` devuelva algo NO conforme crashea el runner al primer uso
(fue el bug histórico de `outlook-desktop`: faltaban ClassVars + health_check y el cursor
era un dict). mypy no cubre `plugins/` (los dirs con guion no son paquetes Python
importables), así que ESTE test es el guard: parametriza sobre los plugins reales del repo,
construye cada Source con una config mínima válida y verifica el contrato `Source`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from memex.core.source import Source

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PLUGINS_DIR = REPO_ROOT / "plugins"

# Config mínima válida por plugin para construir su Source sin tocar red/COM.
MINIMAL_CONFIG: dict[str, dict[str, Any]] = {
    "outlook-desktop": {},
    "selftest": {},
    "imap-university": {
        "server": "imap.test.edu",
        "auth": "basic",
        "username_env": "UNI_IMAP_USER",
        "password_env": "UNI_IMAP_PASS",
        "folders": ["INBOX"],
    },
}


def _plugin_dirs() -> list[Path]:
    if not PLUGINS_DIR.exists():
        return []
    return sorted(p for p in PLUGINS_DIR.iterdir() if p.is_dir() and (p / "__init__.py").exists())


def _load(plugin_dir: Path) -> ModuleType:
    mod_name = f"_contract_{plugin_dir.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_dir / "__init__.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("plugin_dir", _plugin_dirs(), ids=lambda p: p.name)
def test_plugin_build_source_satisfies_contract(
    plugin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Env para que el plugin imap construya su config sin variables vacías.
    monkeypatch.setenv("UNI_IMAP_USER", "alumno@uni.edu")
    monkeypatch.setenv("UNI_IMAP_PASS", "p")

    cfg = MINIMAL_CONFIG.get(plugin_dir.name)
    assert cfg is not None, (
        f"plugin {plugin_dir.name!r} no tiene MINIMAL_CONFIG en este test; "
        "agregá una config mínima válida para que el guard lo cubra"
    )

    mod = _load(plugin_dir)
    src = mod.build_source(cfg)

    assert isinstance(src, Source), (
        f"{plugin_dir.name}.build_source() NO cumple el contrato Source — "
        "faltan ClassVars o métodos: kind / payload_schema / config_schema / "
        "checkpoint_schema / fetch / advance_checkpoint / health_check"
    )
    # El runner construye un cursor fresco con checkpoint_schema(); debe ser instanciable.
    src.checkpoint_schema()
