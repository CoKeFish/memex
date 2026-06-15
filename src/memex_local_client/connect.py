"""Conexión del cliente local a memex: validar + persistir, en un solo paso.

`connect()` valida contra `GET /auth/me` (autenticado, sin efectos) y, si responde,
escribe `~/.memex-local-client/config.toml`. Es la pieza que destraba "no sé cómo
conectarlo": un comando que confirma que el gateway está, que el token (si hace falta)
sirve, y deja la config lista para el daemon.

No vive en `memex.ingestors` (ADR-001): el cliente local sí puede importar el transporte
HTTP compartido (`MemexServerClient`), que es la única superficie contra memex.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from memex.ingestors.memex_server_client import MemexAPIError, MemexServerClient
from memex.logging import get_logger
from memex_local_client.config import LocalConfig
from memex_local_client.paths import ensure_layout

_log = get_logger("memex_local_client.connect")


class ConnectError(Exception):
    """No se pudo validar la conexión (URL inalcanzable, token rechazado, etc.)."""


@dataclass(frozen=True)
class WhoAmI:
    """Identidad que devuelve el servidor en `GET /auth/me`."""

    user_id: int
    email: str
    auth_enforced: bool


def check_connection(gateway_url: str, api_token: str = "") -> WhoAmI:
    """Pega `GET {gateway_url}/auth/me` y devuelve el usuario. No escribe nada.

    Traduce los fallos a `ConnectError` con un mensaje accionable: distingue
    URL/red inalcanzable (no contesta) de token rechazado (401/403).
    """
    base = gateway_url.rstrip("/")
    with MemexServerClient(base, api_token or None, timeout=10.0, max_retries=1) as client:
        try:
            data = client.whoami()
        except MemexAPIError as e:
            if e.status_code in (401, 403):
                raise ConnectError(
                    "el servidor rechazó la credencial (token inválido o falta token). "
                    "Pasá --token con el MEMEX_API_TOKEN del servidor."
                ) from e
            raise ConnectError(f"el servidor respondió {e.status_code}: {e.body or ''}") from e
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise ConnectError(f"no se pudo contactar {base}: {e}") from e
    return WhoAmI(
        user_id=int(data["user_id"]),
        email=str(data.get("email") or ""),
        auth_enforced=bool(data.get("auth_enforced", False)),
    )


def connect(gateway_url: str, api_token: str = "", *, config_path: Path | None = None) -> WhoAmI:
    """Valida la conexión y, si anda, persiste `config.toml`. Idempotente."""
    who = check_connection(gateway_url, api_token)
    ensure_layout()
    written = LocalConfig(gateway_url=gateway_url.rstrip("/"), api_token=api_token).save(
        config_path
    )
    _log.info(
        "memex_local_client.connect.ok",
        gateway_url=gateway_url.rstrip("/"),
        user_id=who.user_id,
        config_path=str(written),
        with_token=bool(api_token),
    )
    return who


def bundled_plugins_dir() -> Path | None:
    """Directorio `plugins/` que viene en el repo (para `setup`). None si no está.

    Resuelve `<repo>/plugins` relativo a este archivo (`src/memex_local_client/connect.py`).
    En un install empaquetado puede no existir → el caller cae a pedir `--from`.
    """
    candidate = Path(__file__).resolve().parents[2] / "plugins"
    return candidate if candidate.exists() else None
