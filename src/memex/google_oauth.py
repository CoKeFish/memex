# mypy: disable-error-code="no-any-return, no-untyped-call"
"""Helper compartido de OAuth2 de Google (glue de google-auth) + el set de scopes de memex.

Hogar NEUTRAL para que TANTO el ingestor IMAP (`memex.ingestors.imap.oauth.google`) COMO el
módulo de calendario (`memex.modules.calendar.providers.oauth`) reusen la MISMA lógica de
cargar/refrescar el token y la MISMA lista de scopes, SIN un import módulo→ingestor (aislamiento
ADR-001).

Decisión 6 (2026-05-30, dueño): una sola autorización pide el conjunto COMPLETO de scopes que
memex usa (Gmail full + Calendar read/write) → un único token reusable entre el ingestor de
correo y el módulo de calendario, y el write-back de calendario NO requiere re-consentir.
Ampliar memex a Tasks/Contacts/Drive = agregar el scope a `GOOGLE_OAUTH_SCOPES` (única fuente
de verdad).

El token (incluye `refresh_token`) es un SECRETO: vive en disco con permisos 0600 (best-effort en
POSIX; Windows lo ignora), nunca en la DB (ADR-015 §7). Las libs de google-auth no están tipadas
(de ahí el `disable-error-code` de arriba), y nunca cruzan fuera de este archivo.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

#: Gmail IMAP requiere el scope full mail ("https://mail.google.com/"); el ".readonly" solo sirve
#: para la REST API de Gmail, no para XOAUTH2 — es una restricción de Google, no una elección.
GMAIL_SCOPE = "https://mail.google.com/"
#: Calendar read/write (NO el ".readonly"): el mismo token sirve para leer y para el write-back.
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"
#: Google Tasks read/write: memex todavía no lo usa, pero el cliente OAuth del dueño ya lo tiene
#: habilitado en la consola y lo pedimos "por si acaso" (decisión 6) para no re-consentir a futuro.
TASKS_SCOPE = "https://www.googleapis.com/auth/tasks"
#: Superset pedido en un solo consentimiento (decisión 6). ÚNICA fuente de verdad de scopes.
GOOGLE_OAUTH_SCOPES = [GMAIL_SCOPE, CALENDAR_SCOPE, TASKS_SCOPE]


class GoogleOAuthError(Exception):
    """Raised cuando un flujo OAuth o el refresh de token de Google falla."""


def authorize_interactive(
    *,
    client_secret_path: str | Path,
    token_path: str | Path,
    scopes: list[str] | None = None,
) -> None:
    """Corre el flujo OAuth2 interactivo (Desktop App: abre el navegador, captura el redirect en
    un puerto local, intercambia el código) y persiste los tokens en `token_path`.

    `scopes` default = `GOOGLE_OAUTH_SCOPES` (todo memex en un consentimiento; decisión 6).
    """
    cs_path = Path(client_secret_path)
    if not cs_path.exists():
        raise GoogleOAuthError(f"client_secret file not found: {cs_path}")
    flow = InstalledAppFlow.from_client_secrets_file(str(cs_path), scopes or GOOGLE_OAUTH_SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True)
    save_credentials(creds, token_path)


def get_access_token(*, token_path: str | Path, scopes: list[str] | None = None) -> str:
    """Carga credenciales, refresca si hace falta, devuelve el access_token string."""
    creds = load_and_refresh(token_path, scopes or GOOGLE_OAUTH_SCOPES)
    if not creds.token:
        raise GoogleOAuthError("no access_token after refresh")
    return creds.token


def load_and_refresh(token_path: str | Path, scopes: list[str] | None = None) -> Credentials:
    """Carga credenciales del disco y las refresca si hace falta, persistiendo el token renovado
    para que las próximas corridas reusen el access_token hasta que expire de nuevo."""
    path = Path(token_path)
    use_scopes = scopes or GOOGLE_OAUTH_SCOPES
    if not path.exists():
        raise GoogleOAuthError(
            f"OAuth token file not found at {path}. Run the authorize flow first."
        )
    try:
        creds = Credentials.from_authorized_user_file(str(path), use_scopes)
    except Exception as e:
        raise GoogleOAuthError(f"failed to load token file {path}: {e}") from e

    if creds.valid:
        return creds

    if not (creds.expired and creds.refresh_token):
        raise GoogleOAuthError(
            f"OAuth credentials at {path} are invalid and cannot be refreshed "
            "(missing refresh_token or unexpired but invalid). Re-run authorize."
        )

    try:
        creds.refresh(Request())
    except Exception as e:
        raise GoogleOAuthError(f"token refresh failed for {path}: {e}") from e

    save_credentials(creds, path)
    return creds


def save_credentials(creds: Credentials, token_path: str | Path) -> None:
    """Persiste las credenciales (formato JSON de google-auth) con permisos 0600 best-effort."""
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(creds.to_json(), encoding="utf-8")
    with contextlib.suppress(OSError):
        # Best-effort tighten en POSIX; Windows lanza y lo ignoramos.
        path.chmod(0o600)
