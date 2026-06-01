"""Flujo web "Conectar con Google" (OAuth2 Authorization Code) para Gmail IMAP.

`start` arma la URL de consentimiento con un `state` firmado (HMAC + exp, sin tabla). `callback`
valida el state + la sesión, intercambia el `code`, y guarda el token (self-contained, con
refresh_token) CIFRADO en el vault + el email como `username`, y deja la source IMAP de Gmail lista.

El `redirect_base_url` es el ORIGEN público del dashboard: el callback se registra en Google como
`{base}/api/oauth/google/callback` (el proxy/`/api` lo rutea al backend) y al terminar redirige a
`{base}/cuenta` (el frontend). Así sirve igual en dev (Vite) y en prod (reverse proxy).
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool

from memex import google_oauth
from memex.api.auth import current_user_id
from memex.api.routers.accounts import _perform_health_check
from memex.api.schemas import OAuthStartResponse
from memex.config import settings
from memex.db import connection
from memex.logging import get_logger
from memex.security import crypto, vault

router = APIRouter(tags=["oauth"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.oauth")

# Code verifiers PKCE en vuelo (state nonce → verifier). NUNCA viajan en la URL. En memoria: se
# pierden al reiniciar el API (el flujo en curso falla y se reintenta). Coherente con 1 worker.
_PENDING_VERIFIERS: dict[str, tuple[str, float]] = {}
_VERIFIER_TTL = 600.0


def _remember_verifier(nonce: str, verifier: str) -> None:
    now = time.time()
    for stale in [k for k, (_, exp) in _PENDING_VERIFIERS.items() if exp < now]:
        _PENDING_VERIFIERS.pop(stale, None)
    _PENDING_VERIFIERS[nonce] = (verifier, now + _VERIFIER_TTL)


def _pop_verifier(nonce: str) -> str | None:
    item = _PENDING_VERIFIERS.pop(nonce, None)
    if item is None:
        return None
    verifier, exp = item
    return verifier if exp >= time.time() else None


def _assert_owns_account(conn: Any, user_id: int, account_id: int) -> None:
    owner = conn.execute(
        text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": account_id}
    ).scalar()
    if owner != user_id:
        raise HTTPException(status_code=404, detail="account not found")


def _callback_redirect_uri() -> str:
    return settings.oauth_redirect_base_url.rstrip("/") + "/api/oauth/google/callback"


def _dashboard_url(query: str) -> str:
    return f"{settings.oauth_redirect_base_url.rstrip('/')}/cuenta?{query}"


def _oauth_configured() -> bool:
    return bool(settings.google_oauth_client_secret_json and settings.oauth_redirect_base_url)


@router.get("/accounts/{account_id}/oauth/google/start", response_model=OAuthStartResponse)
async def google_start(account_id: int, user_id: UserID) -> dict[str, str]:
    if not _oauth_configured():
        raise HTTPException(
            status_code=503,
            detail="OAuth de Google no configurado (client_secret web / redirect base url)",
        )
    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)

    nonce = secrets.token_urlsafe(8)
    state = crypto.sign_state(
        {"account_id": account_id, "user_id": user_id, "nonce": nonce}, now=int(time.time())
    )
    try:
        url, code_verifier = google_oauth.start_authorization(
            client_secret_path=settings.google_oauth_client_secret_json,
            redirect_uri=_callback_redirect_uri(),
            state=state,
        )
    except google_oauth.GoogleOAuthError as e:
        raise HTTPException(status_code=503, detail=f"OAuth de Google no configurado: {e}") from e
    _remember_verifier(nonce, code_verifier)
    _log.info("oauth.google.start", user_id=user_id, account_id=account_id)
    return {"authorization_url": url}


def _exchange_and_profile(state: str, code: str, code_verifier: str) -> tuple[str, str]:
    """Bloqueante (red): intercambia el code (con el verifier del start) y trae el email."""
    token_json = google_oauth.complete_exchange(
        client_secret_path=settings.google_oauth_client_secret_json,
        redirect_uri=_callback_redirect_uri(),
        state=state,
        code=code,
        code_verifier=code_verifier,
    )
    access_token = google_oauth.access_token_from_json(token_json)
    email = google_oauth.gmail_address(access_token)
    return token_json, email


def _ensure_gmail_source(conn: Any, user_id: int, account_id: int, email: str) -> None:
    """Crea/actualiza la source IMAP de Gmail (oauth2) vinculada a la cuenta. Los `*_env` son claves
    que el resolver llena desde el vault (token + username); no son env vars reales."""
    config = {
        "server": "imap.gmail.com",
        "port": 993,
        "auth": "oauth2",
        "oauth_provider": "google",
        "username_env": f"MEMEX_IMAP_USER_ACCT_{account_id}",
        "oauth_token_env": f"MEMEX_OAUTH_TOKEN_ACCT_{account_id}",
        "folders": ["INBOX"],
    }
    conn.execute(
        text(
            """
            INSERT INTO sources (user_id, name, type, config, account_id)
            VALUES (:u, :n, 'imap', CAST(:cfg AS JSONB), :aid)
            ON CONFLICT (user_id, name)
                DO UPDATE SET config = EXCLUDED.config, account_id = EXCLUDED.account_id
            """
        ),
        {"u": user_id, "n": f"gmail-{account_id}", "cfg": json.dumps(config), "aid": account_id},
    )


@router.get("/oauth/google/callback")
async def google_callback(
    user_id: UserID,
    state: Annotated[str, Query()],
    code: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    if error:
        return RedirectResponse(_dashboard_url(f"oauth_error={error}"), status_code=302)
    if not code:
        return RedirectResponse(_dashboard_url("oauth_error=missing_code"), status_code=302)
    try:
        payload = crypto.verify_state(state, now=int(time.time()))
    except crypto.StateError:
        return RedirectResponse(_dashboard_url("oauth_error=bad_state"), status_code=302)

    account_id = int(payload["account_id"])
    if int(payload["user_id"]) != user_id:
        # Confused-deputy: la sesión actual no es la que inició el flujo.
        return RedirectResponse(_dashboard_url("oauth_error=user_mismatch"), status_code=302)

    code_verifier = _pop_verifier(str(payload.get("nonce", "")))
    if code_verifier is None:
        return RedirectResponse(_dashboard_url("oauth_error=expired"), status_code=302)

    try:
        token_json, email = await run_in_threadpool(
            _exchange_and_profile, state, code, code_verifier
        )
    except google_oauth.GoogleOAuthError as e:
        _log.warning("oauth.google.exchange_failed", account_id=account_id, reason=str(e))
        return RedirectResponse(_dashboard_url("oauth_error=exchange_failed"), status_code=302)

    with connection() as conn:
        _assert_owns_account(conn, user_id, account_id)
        vault.set_secret(conn, account_id, "google_oauth_token", token_json)
        vault.set_secret(conn, account_id, "username", email)
        _ensure_gmail_source(conn, user_id, account_id, email)
        conn.execute(
            text(
                "UPDATE accounts SET provider = 'google', "
                "metadata = metadata || jsonb_build_object('email', CAST(:em AS TEXT)) "
                "WHERE id = :aid"
            ),
            {"em": email, "aid": account_id},
        )
    # Auto-validar UNA vez la cuenta recién conectada (best-effort; no rompe el connect si falla).
    # El botón "Validar" del dashboard queda para los re-chequeos manuales posteriores.
    try:
        await _perform_health_check(account_id, user_id)
    except Exception as e:
        _log.warning("oauth.google.autovalidate_failed", account_id=account_id, reason=str(e))

    _log.info("oauth.google.connected", user_id=user_id, account_id=account_id)
    return RedirectResponse(_dashboard_url("connected=google"), status_code=302)
