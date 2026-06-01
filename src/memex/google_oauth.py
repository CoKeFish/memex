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
import json
import os
from pathlib import Path

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow  # type: ignore[import-untyped]

# Google puede devolver scopes ADICIONALES ya concedidos antes (include_granted_scopes); sin esto
# oauthlib trata el "scope cambió" como error. Relajarlo es lo estándar/recomendado para ese caso.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

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


# ----- Flujo WEB (Authorization Code) para el botón del dashboard ----------- #
# A diferencia del Desktop/CLI de arriba, NO toca disco: el token se devuelve como JSON y lo guarda
# el caller en el vault; el access_token se refresca en memoria al usarlo.


def build_web_flow(
    *,
    client_secret_path: str | Path,
    redirect_uri: str,
    state: str | None = None,
    scopes: list[str] | None = None,
    code_verifier: str | None = None,
) -> Flow:
    """Arma un `Flow` web desde el client_secret.json. Si se pasa `code_verifier` (PKCE) lo fija en
    vez de autogenerar (para que el callback intercambie con el MISMO verifier)."""
    cs_path = Path(client_secret_path)
    if not cs_path.exists():
        raise GoogleOAuthError(f"client_secret file not found: {cs_path}")
    extra: dict[str, object] = {}
    if code_verifier is not None:
        extra["code_verifier"] = code_verifier
        extra["autogenerate_code_verifier"] = False
    flow = Flow.from_client_secrets_file(
        str(cs_path), scopes=scopes or GOOGLE_OAUTH_SCOPES, state=state, **extra
    )
    flow.redirect_uri = redirect_uri
    return flow


def start_authorization(
    *, client_secret_path: str | Path, redirect_uri: str, state: str
) -> tuple[str, str]:
    """Devuelve `(authorization_url, code_verifier)`. El `code_verifier` (PKCE) lo guarda el caller
    server-side y lo vuelve a pasar en `complete_exchange` (NUNCA va en la URL). offline +
    prompt=consent garantizan refresh_token."""
    flow = build_web_flow(
        client_secret_path=client_secret_path, redirect_uri=redirect_uri, state=state
    )
    url, _state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent"
    )
    return str(url), str(flow.code_verifier)


def complete_exchange(
    *, client_secret_path: str | Path, redirect_uri: str, state: str, code: str, code_verifier: str
) -> str:
    """Intercambia el `code` (con el MISMO `code_verifier` del start) → JSON de credenciales."""
    flow = build_web_flow(
        client_secret_path=client_secret_path,
        redirect_uri=redirect_uri,
        state=state,
        code_verifier=code_verifier,
    )
    try:
        flow.fetch_token(code=code)
    except Exception as e:
        raise GoogleOAuthError(f"token exchange failed: {e}") from e
    return str(flow.credentials.to_json())


def access_token_from_json(token_json: str, scopes: list[str] | None = None) -> str:
    """Carga credenciales del JSON (vault), refresca en memoria si hace falta, devuelve el token."""
    try:
        info = json.loads(token_json)
        creds = Credentials.from_authorized_user_info(info, scopes or GOOGLE_OAUTH_SCOPES)
    except Exception as e:
        raise GoogleOAuthError(f"invalid token json: {e}") from e
    if not creds.valid:
        if not (creds.expired and creds.refresh_token):
            raise GoogleOAuthError(
                "OAuth token inválido y sin refresh_token; re-conectá con Google."
            )
        try:
            creds.refresh(Request())
        except Exception as e:
            raise GoogleOAuthError(f"token refresh failed: {e}") from e
    if not creds.token:
        raise GoogleOAuthError("no access_token after refresh")
    return str(creds.token)


def gmail_address(access_token: str) -> str:
    """Email de la cuenta vía Gmail profile API (usa el scope Gmail que ya tenemos; sin openid)."""
    try:
        resp = httpx.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/profile",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        email = resp.json().get("emailAddress")
    except Exception as e:
        raise GoogleOAuthError(f"failed to fetch Gmail address: {e}") from e
    if not email:
        raise GoogleOAuthError("Gmail profile had no emailAddress")
    return str(email)
