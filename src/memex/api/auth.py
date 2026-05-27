from typing import Annotated

from fastapi import Header, HTTPException

from memex.config import settings

DEFAULT_DEV_USER_ID = 1


async def current_user_id(
    authorization: Annotated[str | None, Header()] = None,
) -> int:
    """Resolve user_id for the request.

    - MEMEX_AUTH_ENFORCED=false: always returns DEFAULT_DEV_USER_ID.
    - true (single-user token mode): validates Bearer against MEMEX_API_TOKEN,
      returns user 1 (placeholder until a user_tokens table exists).
    - Multi-user real (future): lookup token → user_id from DB or JWT claim.
    """
    if not settings.auth_enforced:
        return DEFAULT_DEV_USER_ID
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer")
    token = authorization[len("Bearer ") :]
    if not settings.api_token or token != settings.api_token:
        raise HTTPException(status_code=403, detail="invalid token")
    return DEFAULT_DEV_USER_ID
