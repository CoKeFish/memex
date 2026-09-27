"""Token OAuth del proveedor de contactos, resuelto desde el VAULT de la cuenta del dashboard.

A diferencia de calendar (que lee el token de un archivo en disco apuntado por una env var), el
módulo `identidades` reusa el MISMO token Google que el dashboard ya guardó cifrado en el vault al
conectar la cuenta (`account_secrets["google_oauth_token"]`, ver `api/routers/oauth.py`).
Decisión 6: una sola autorización pide TODOS los scopes (Gmail + Calendar + Tasks + Contacts) → un
único token para todo memex. ⚠️ El scope de Contacts es nuevo: los tokens emitidos ANTES no lo
tienen → hay que
re-conectar Google una vez en `/cuenta`.

NO hay import módulo→ingestor (ADR-001): el glue de google-auth vive en `memex.google_oauth`
(neutral); el descifrado del vault vive en `memex.security.vault` (que sí puede tocar `memex.db`).
El caller (sync worker / CLI) abre la conexión y la pasa acá; este módulo no abre una propia.
"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from memex import google_oauth
from memex.modules.identidades.providers.base import ContactsProviderError
from memex.security import vault

_TOKEN_SECRET_NAME = "google_oauth_token"


def access_token_from_vault(conn: Connection, account_id: int) -> str:
    """Descifra el token Google de la cuenta desde el vault, lo refresca en memoria si hace falta y
    devuelve el access_token string. Lanza `ContactsProviderError` si la cuenta no tiene token
    guardado (no conectó Google) o si el refresh falla."""
    try:
        secrets = vault.get_account_secrets(conn, account_id)
    except vault.VaultError as e:
        raise ContactsProviderError(0, f"vault: {e}") from e
    token_json = secrets.get(_TOKEN_SECRET_NAME)
    if not token_json:
        raise ContactsProviderError(
            0,
            f"la cuenta {account_id} no tiene token Google en el vault — conectá Google en /cuenta",
        )
    try:
        return google_oauth.access_token_from_json(token_json)
    except google_oauth.GoogleOAuthError as e:
        raise ContactsProviderError(0, f"oauth: {e}") from e


def access_token(provider: str, *, conn: Connection, account_id: int) -> str:
    """Mint/refresh de un access token para `provider`, desde el token del vault de la cuenta.

    Slice 1: solo `google`. Lanza `ContactsProviderError` si el proveedor no tiene resolver OAuth.
    """
    if provider == "google":
        return access_token_from_vault(conn, account_id)
    raise ContactsProviderError(0, f"no OAuth resolver for contacts provider {provider!r}")
