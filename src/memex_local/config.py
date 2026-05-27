from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from memex_local.paths import main_config_path


class LocalConfigError(Exception):
    """Config del cliente local inválida o ausente."""


@dataclass(frozen=True)
class LocalConfig:
    """Config principal del daemon — apunta al bridge de memex.

    Se carga desde `~/.memex-local/config.toml` o se sobreescribe con env vars
    (MEMEX_LOCAL_BRIDGE_URL, MEMEX_LOCAL_TOKEN). Los plugins tienen su propio
    config TOML por separado.

    Los paths concretos del bridge (`/bridge/plugins/<name>/state`, etc.) son
    fijos — los conoce `BridgeClient`. Acá solo decimos a qué host hablar.
    """

    bridge_url: str
    api_token: str

    @classmethod
    def load(cls, path: Path | None = None) -> LocalConfig:
        path = path or main_config_path()
        data: dict[str, object] = {}
        if path.exists():
            with path.open("rb") as f:
                data = tomllib.load(f)

        bridge_url = str(
            os.environ.get("MEMEX_LOCAL_BRIDGE_URL") or data.get("bridge_url") or ""
        ).strip()
        if not bridge_url:
            raise LocalConfigError(
                "missing bridge_url. Set MEMEX_LOCAL_BRIDGE_URL or define " f"bridge_url in {path}"
            )

        api_token = str(os.environ.get("MEMEX_LOCAL_TOKEN") or data.get("api_token") or "").strip()
        return cls(bridge_url=bridge_url, api_token=api_token)
