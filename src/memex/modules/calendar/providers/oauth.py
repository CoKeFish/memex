"""Tokens OAuth por proveedor de calendario, vía el helper NEUTRAL `memex.google_oauth`.

Despacha por nombre de proveedor (slice 1: solo `google`; Outlook/MS Graph llega después, hoy no
resuelto). Usa el conjunto COMPLETO de scopes de memex (decisión 6) → el mismo token consolidado
sirve para el ingestor IMAP y para el sync de calendario; el usuario consiente UNA sola vez.

Dos orígenes del token (0100):
- **vault** (preferido): la cuenta de calendario apunta a la cuenta del dashboard (`account_id`) que
  ya conectó Google en `/cuenta`; el token se descifra de `account_secrets["google_oauth_token"]`
  (igual que `identidades.providers.oauth`). No hay que autorizar nada por consola.
- **archivo** (legacy, VPS sin dashboard): `token_path_env` → path del JSON; se autoriza con
  `memex-calendar-sync authorize`.

NO hay import módulo→ingestor (ADR-001): el glue de google-auth vive en `memex.google_oauth`,
neutral, compartido con `memex.ingestors.imap.oauth.google`; el descifrado del vault vive en
`memex.security.vault`.
"""

from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy.engine import Connection

from memex import google_oauth
from memex.modules.calendar.providers.base import CalendarProviderError
from memex.security import vault

#: Nombre del secreto del vault que deja `api/routers/oauth.py` al «Conectar con Google».
TOKEN_SECRET_NAME = "google_oauth_token"


def access_token_from_vault(conn: Connection, account_id: int) -> str:
    """Descifra el token Google de la cuenta del dashboard, lo refresca en memoria si hace falta y
    devuelve el access token. `CalendarProviderError` si la cuenta no conectó Google o el refresh
    falla (token revocado → reconectar en /cuenta)."""
    try:
        secrets = vault.get_account_secrets(conn, account_id)
    except vault.VaultError as e:
        raise CalendarProviderError(0, f"vault: {e}") from e
    token_json = secrets.get(TOKEN_SECRET_NAME)
    if not token_json:
        raise CalendarProviderError(
            0,
            f"la cuenta {account_id} no tiene token Google en el vault: conectá Google en /cuenta",
        )
    try:
        return google_oauth.access_token_from_json(token_json)
    except google_oauth.GoogleOAuthError as e:
        raise CalendarProviderError(0, f"oauth (vault, cuenta {account_id}): {e}") from e


def secrets_dir() -> Path:
    """Carpeta gitignored del repo donde viven los secretos OAuth (client_secret + tokens).

    Default: `<repo>/secrets/` (ya está en `.gitignore`; el repo es PÚBLICO). Override con
    `MEMEX_SECRETS_DIR` (ej. una ruta distinta en el VPS)."""
    override = os.environ.get("MEMEX_SECRETS_DIR", "").strip()
    if override:
        return Path(override)
    # cli.py/oauth.py viven en src/memex/modules/calendar/providers → parents[5] = raíz del repo.
    return Path(__file__).resolve().parents[5] / "secrets"


def default_client_secret_path() -> Path:
    """Path por default del client_secret.json (en `<repo>/secrets/`)."""
    return secrets_dir() / "google_client_secret.json"


def resolve_token_path(provider: str, account_id: int, token_path_env: str | None) -> str:
    """Resuelve dónde vive el token OAuth de una cuenta, SIN obligar a env vars.

    1) si `token_path_env` nombra una env var SETEADA → se usa (override avanzado / VPS);
    2) si no → default repo-local `<repo>/secrets/<provider>_calendar_<account_id>.json`.
    Así el caso simple no necesita ninguna env var y nada queda fuera del repo."""
    if token_path_env:
        value = os.environ.get(token_path_env, "").strip()
        if value:
            return value
    return str(secrets_dir() / f"{provider}_calendar_{account_id}.json")


def access_token(provider: str, *, token_path: str | Path) -> str:
    """Mint/refresh de un access token para `provider`, desde el token persistido en disco.

    Lanza `CalendarProviderError` si el proveedor no tiene resolver OAuth o si el flujo falla
    (token ausente/inválido) — el worker la atrapa como cualquier error de proveedor.
    """
    if provider == "google":
        try:
            return google_oauth.get_access_token(token_path=token_path)
        except google_oauth.GoogleOAuthError as e:
            raise CalendarProviderError(0, f"oauth: {e}") from e
    raise CalendarProviderError(0, f"no OAuth resolver for calendar provider {provider!r}")


def authorize(provider: str, *, client_secret_path: str | Path, token_path: str | Path) -> None:
    """Corre el flujo OAuth interactivo (browser) para `provider`, pidiendo el set COMPLETO de
    scopes de memex (decisión 6) y persistiendo el token en `token_path`."""
    if provider == "google":
        try:
            google_oauth.authorize_interactive(
                client_secret_path=client_secret_path, token_path=token_path
            )
        except google_oauth.GoogleOAuthError as e:
            raise CalendarProviderError(0, f"oauth authorize: {e}") from e
        return
    raise CalendarProviderError(0, f"no OAuth flow for calendar provider {provider!r}")
