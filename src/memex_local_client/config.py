from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from memex_local_client.paths import main_config_path


class LocalConfigError(Exception):
    """Config del cliente local inválida o ausente."""


@dataclass(frozen=True)
class LocalConfig:
    """Config principal del daemon — apunta al gateway de memex.

    Se carga desde `~/.memex-local-client/config.toml` o se sobreescribe con env vars
    (MEMEX_LOCAL_GATEWAY_URL, MEMEX_LOCAL_TOKEN). Los plugins tienen su propio
    config TOML por separado.

    Los paths concretos del gateway (`/gateway/plugins/<name>/state`, etc.) son
    fijos — los conoce `GatewayClient`. Acá solo decimos a qué host hablar.
    """

    gateway_url: str
    api_token: str

    @classmethod
    def load(cls, path: Path | None = None) -> LocalConfig:
        path = path or main_config_path()
        data: dict[str, object] = {}
        if path.exists():
            with path.open("rb") as f:
                data = tomllib.load(f)

        gateway_url = str(
            os.environ.get("MEMEX_LOCAL_GATEWAY_URL") or data.get("gateway_url") or ""
        ).strip()
        if not gateway_url:
            raise LocalConfigError(
                f"missing gateway_url. Set MEMEX_LOCAL_GATEWAY_URL or define gateway_url in {path}"
            )

        api_token = str(os.environ.get("MEMEX_LOCAL_TOKEN") or data.get("api_token") or "").strip()
        return cls(gateway_url=gateway_url, api_token=api_token)
