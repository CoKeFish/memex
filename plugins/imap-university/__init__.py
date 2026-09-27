"""Plugin de cliente local: correo de la universidad por IMAP.

Reusa `ImapSource` y `ImapConfig` de `memex.ingestors.imap` sin modificarlos —
el plugin es un envoltorio fino que:

1. Toma el config TOML local (`~/.memex-local-client/plugins/imap-university/config.toml`).
2. Lo pasa por `ImapConfig.from_source_config` para resolver env vars.
3. Devuelve un `ImapSource` listo para el runner.

Soporta auth básica y OAuth2 — el plugin no decide cuál: lee `auth` del
config y delega en la maquinaria existente del ingestor IMAP.

Para autorizar OAuth la primera vez: `memex-local-client plugin authorize imap-university`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from memex.core.source import Source
from memex.ingestors.imap.config import ImapConfig, ImapConfigError
from memex.ingestors.imap.source import ImapSource

name = "imap-university"
version = "0.1.0"
source_type = "imap"
default_schedule = "PT5M"


def build_source(local_config: Mapping[str, Any]) -> Source:
    cfg = dict(local_config)
    # Contrato de backfill del cliente local: `backfill_since`/`backfill_until` (ISO) → el modo
    # `range` que ImapSource ya soporta (SINCE/BEFORE, NO toca el checkpoint). Tomamos la parte
    # fecha del ISO porque IMAP filtra por día.
    bsince = cfg.pop("backfill_since", None)
    buntil = cfg.pop("backfill_until", None)
    if bsince:
        cfg["fetch_mode"] = "range"
        cfg["fetch_since"] = str(bsince)[:10]
        if buntil:
            cfg["fetch_until"] = str(buntil)[:10]
    return ImapSource(ImapConfig.from_source_config(cfg))


def identity(local_config: Mapping[str, Any]) -> str | None:
    """Email/usuario IMAP de la cuenta — el login con el que el plugin se conecta.

    memex lo usa solo para saber de qué buzón vienen los correos (no es secreto: el
    daemon lo reporta al gateway). Resuelve el env var `username_env`; None si no está.
    """
    var = local_config.get("username_env")
    if not var:
        return None
    return os.environ.get(str(var), "").strip() or None


def validate_requirements(local_config: Mapping[str, Any]) -> list:
    """Chequea que los env vars referidos por el config existan y no estén vacíos."""
    from memex_local_client.protocol import Problem

    problems: list[Problem] = []

    if "server" not in local_config:
        problems.append(Problem("error", "missing-server", "config.toml debe declarar 'server'"))
    if "username_env" not in local_config:
        problems.append(
            Problem("error", "missing-username-env", "config.toml debe declarar 'username_env'")
        )
    else:
        var = str(local_config["username_env"])
        if not os.environ.get(var, "").strip():
            problems.append(Problem("error", "env-empty", f"env var {var!r} no definida o vacía"))

    auth = str(local_config.get("auth", "basic"))
    if auth == "basic":
        pw_env = local_config.get("password_env")
        if not pw_env:
            problems.append(
                Problem("error", "missing-password-env", "auth=basic requiere 'password_env'")
            )
        elif not os.environ.get(str(pw_env), "").strip():
            problems.append(
                Problem("error", "env-empty", f"env var {pw_env!r} no definida o vacía")
            )
    elif auth == "oauth2":
        for key in ("oauth_client_secret_path_env", "oauth_token_path_env", "oauth_provider"):
            if not local_config.get(key):
                problems.append(Problem("error", f"missing-{key}", f"auth=oauth2 requiere '{key}'"))

    # Intento dry de construir la config; si falla, devolver el error formateado.
    try:
        ImapConfig.from_source_config(dict(local_config))
    except ImapConfigError as e:
        problems.append(Problem("error", "config-invalid", str(e)))

    return problems


def authorize_interactive(local_config: Mapping[str, Any]) -> None:
    """Dispara el flujo OAuth2 si el plugin está configurado para auth=oauth2.

    Reusa el registry de proveedores OAuth ya existente (`memex.ingestors.imap.oauth`).
    """
    if local_config.get("auth") != "oauth2":
        raise RuntimeError("authorize_interactive solo aplica a plugins con auth='oauth2'")

    from memex.ingestors.imap import oauth as oauth_registry

    provider_name = str(local_config.get("oauth_provider") or "")
    if not provider_name:
        raise RuntimeError(
            "config.toml debe declarar 'oauth_provider' "
            f"(known: {oauth_registry.known_providers()})"
        )
    provider = oauth_registry.resolve(provider_name)

    cs_env = str(local_config["oauth_client_secret_path_env"])
    token_env = str(local_config["oauth_token_path_env"])
    cs_path = os.environ.get(cs_env)
    token_path = os.environ.get(token_env)
    if not cs_path:
        raise RuntimeError(f"env var {cs_env!r} no definida")
    if not token_path:
        raise RuntimeError(f"env var {token_env!r} no definida")

    provider.authorize_interactive(client_secret_path=cs_path, token_path=token_path)
