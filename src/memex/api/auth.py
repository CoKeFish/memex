from typing import Annotated

from fastapi import Header, HTTPException

from memex.config import settings
from memex.logging import bind_request_context, get_logger

DEFAULT_DEV_USER_ID = 1

_log = get_logger("memex.api.auth")


async def current_user_id(
    authorization: Annotated[str | None, Header()] = None,
) -> int:
    """Resolve user_id for the request.

    - MEMEX_AUTH_ENFORCED=false: always returns DEFAULT_DEV_USER_ID.
    - true (single-user token mode): validates Bearer against MEMEX_API_TOKEN,
      returns user 1 (placeholder until a user_tokens table exists).
    - Multi-user real (future): lookup token → user_id from DB or JWT claim.

    Side effect: binds resolved user_id to structlog contextvars so all
    downstream logs in this request automatically carry it.
    """
    if not settings.auth_enforced:
        bind_request_context(user_id=DEFAULT_DEV_USER_ID)
        return DEFAULT_DEV_USER_ID
    if not authorization or not authorization.startswith("Bearer "):
        _log.info("auth.rejected", reason="missing_bearer")
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization[len("Bearer ") :]
    if not settings.api_token or token != settings.api_token:
        _log.info("auth.rejected", reason="invalid_token")
        raise HTTPException(status_code=403, detail="invalid token")
    bind_request_context(user_id=DEFAULT_DEV_USER_ID)
    return DEFAULT_DEV_USER_ID
