"""Seam de resolución: inyecta los secretos del vault en el `env` de un ingestor.

Server-side, antes de instanciar un `Source`, expone los secretos descifrados de la cuenta vinculada
BAJO EL MISMO nombre de env var que el `cfg` referencia (o el default del ingestor). Así el ingestor
los resuelve por su patrón env-var-by-name sin saber que vinieron de la DB → preserva el aislamiento
de ADR-001 (el descifrado vive acá, NO en el ingestor).

Como el descifrado usa la master key del servidor, esto funciona SIN sesión del usuario: la ingesta
desatendida (fetch agendado, streaming) puede correr aunque nadie esté logueado.

El vault es la ÚNICA fuente de las CREDENCIALES de ingestor (`_SECRET_ENV`): se QUITAN del
`env` base aunque estén en `.env`/host, y solo se reinyectan si el vault de la cuenta las tiene.
El `.env` NO es fuente de credenciales — sin la credencial en el vault la env var queda ausente y
el ingestor falla con error claro. El resto de `os.environ` (infra: DB, MinIO, keys) pasa intacto.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from sqlalchemy import Connection

from memex.logging import get_logger
from memex.security import crypto, vault

_log = get_logger("memex.sources.resolver")

# source_type -> {secreto del vault: (campo del cfg con el nombre de env var, default)}.
# Debe espejar cómo cada `from_source_config` resuelve el nombre de la env var:
#   imap  -> cfg["username_env"] / cfg["password_env"] (requeridos, sin default).
#   telegram -> cfg.get("api_id_env") or "MEMEX_TG_API_ID" (y api_hash/phone análogos).
#   social   -> cfg.get("apify_token_env") or "MEMEX_APIFY_TOKEN".
# Los secretos OAuth de IMAP y la sesión de Telegram son ARCHIVOS (no se migran al vault).
_SECRET_ENV: dict[str, dict[str, tuple[str, str | None]]] = {
    "imap": {
        "username": ("username_env", None),
        "password": ("password_env", None),
        # Token OAuth web (JSON self-contained del vault) → inyectado bajo `oauth_token_env`.
        "google_oauth_token": ("oauth_token_env", None),
    },
    "telegram": {
        "api_id": ("api_id_env", "MEMEX_TG_API_ID"),
        "api_hash": ("api_hash_env", "MEMEX_TG_API_HASH"),
        "phone": ("phone_env", "MEMEX_TG_PHONE"),
    },
    "instagram": {"apify_token": ("apify_token_env", "MEMEX_APIFY_TOKEN")},
    "facebook": {"apify_token": ("apify_token_env", "MEMEX_APIFY_TOKEN")},
    "x": {"apify_token": ("apify_token_env", "MEMEX_APIFY_TOKEN")},
}


def _credential_env_vars(source_type: str, cfg: dict[str, Any]) -> set[str]:
    """Nombres de env var bajo los que viven las CREDENCIALES de este `source_type` (override de
    `cfg` o default de `_SECRET_ENV`). El vault es la única fuente: esto se quita del env base."""
    mapping = _SECRET_ENV.get(source_type, {})
    names = {
        str(cfg.get(env_field) or default_env or "")
        for _, (env_field, default_env) in mapping.items()
    }
    names.discard("")
    return names


def build_resolved_env(
    conn: Connection,
    *,
    user_id: int,
    source_type: str,
    cfg: dict[str, Any],
    account_id: int | None,
) -> Mapping[str, str]:
    """Devuelve `os.environ` con las credenciales de ingestor resueltas SOLO desde el vault.

    Las env var de credenciales (`_SECRET_ENV`) se QUITAN del env base —aunque vengan del
    `.env`/host— y solo se reinyectan si el vault de la cuenta las tiene. Sin la credencial en el
    vault la env var queda ausente y el ingestor falla con error claro: el `.env` nunca es fuente de
    credenciales. El resto de `os.environ` (infra: DB, MinIO, keys) pasa intacto.
    """
    env = dict(os.environ)
    mapping = _SECRET_ENV.get(source_type)
    if not mapping:
        return env

    # El vault es la ÚNICA fuente: las credenciales no se heredan del `.env`/host.
    for env_var in _credential_env_vars(source_type, cfg):
        env.pop(env_var, None)

    if account_id is None:
        return env
    try:
        secrets = vault.get_account_secrets(conn, account_id)
    except vault.UserVaultMissingError:
        # Cuenta sin vault provisionado → sin credenciales (NO se cae al `.env`).
        return env
    except crypto.MasterKeyMissingError:
        _log.warning(
            "resolver.master_key_missing",
            user_id=user_id,
            account_id=account_id,
            source_type=source_type,
        )
        return env

    injected = 0
    for secret_name, (env_field, default_env) in mapping.items():
        if secret_name not in secrets:
            continue
        env_var = str(cfg.get(env_field) or default_env or "")
        if not env_var:
            continue
        env[env_var] = secrets[secret_name]
        injected += 1
    if injected:
        _log.info(
            "resolver.vault_injected",
            user_id=user_id,
            account_id=account_id,
            source_type=source_type,
            count=injected,
        )
    return env
