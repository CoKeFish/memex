from typing import Annotated

from fastapi import Cookie, Header, HTTPException

from memex.config import settings
from memex.db import connection
from memex.logging import bind_request_context, get_logger
from memex.security import sessions

DEFAULT_DEV_USER_ID = 1

_log = get_logger("memex.api.auth")


def _resolve_session(token: str) -> int | None:
    """Valida una cookie de sesión contra la DB. Devuelve el user_id o None."""
    with connection() as conn:
        return sessions.validate_session(conn, token)


async def current_user_id(
    authorization: Annotated[str | None, Header()] = None,
    session_cookie: Annotated[str | None, Cookie(alias=settings.cookie_name)] = None,
) -> int:
    """Resolve user_id for the request.

    - MEMEX_AUTH_ENFORCED=false: always returns DEFAULT_DEV_USER_ID (dev single-user).
    - true: resolves identity in order:
        1. Session cookie (interactive dashboard login) → its user_id. The session is
           ONLY auth — it carries no decryption key; the server decrypts vault secrets
           with the global master key regardless of who is logged in.
        2. Bearer against MEMEX_API_TOKEN (machine/CLI access) → user 1. Does NOT unlock
           anything extra; vault decryption is master-key based, not session based.

    Side effect: binds resolved user_id to structlog contextvars so all downstream logs
    in this request automatically carry it.
    """
    if not settings.auth_enforced:
        bind_request_context(user_id=DEFAULT_DEV_USER_ID)
        return DEFAULT_DEV_USER_ID

    # 1. Cookie de sesión (login interactivo). Si es inválida/expirada, caemos al bearer
    #    (no rompemos clientes máquina); si tampoco hay bearer, 401 abajo.
    if session_cookie:
        uid = _resolve_session(session_cookie)
        if uid is not None:
            bind_request_context(user_id=uid)
            return uid

    # 2. Bearer (máquina). Conserva exactamente los códigos previos (401 sin bearer, 403 inválido).
    if not authorization or not authorization.startswith("Bearer "):
        _log.info("auth.rejected", reason="missing_bearer")
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization[len("Bearer ") :]
    if not settings.api_token or token != settings.api_token:
        _log.info("auth.rejected", reason="invalid_token")
        raise HTTPException(status_code=403, detail="invalid token")
    bind_request_context(user_id=DEFAULT_DEV_USER_ID)
    return DEFAULT_DEV_USER_ID
