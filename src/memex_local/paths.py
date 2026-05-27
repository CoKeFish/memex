from __future__ import annotations

import os
from pathlib import Path


def home_dir() -> Path:
    """Directorio raíz del cliente local. Override con MEMEX_LOCAL_HOME."""
    override = os.environ.get("MEMEX_LOCAL_HOME")
    if override:
        return Path(override)
    return Path.home() / ".memex-local"


def plugins_dir() -> Path:
    return home_dir() / "plugins"


def secrets_dir() -> Path:
    return home_dir() / "secrets"


def state_db_path() -> Path:
    return home_dir() / "state.db"


def main_config_path() -> Path:
    return home_dir() / "config.toml"


def ensure_layout() -> None:
    """Crea el árbol de directorios si no existe. Idempotente."""
    home_dir().mkdir(parents=True, exist_ok=True)
    plugins_dir().mkdir(parents=True, exist_ok=True)
    secrets_dir().mkdir(parents=True, exist_ok=True)
