from __future__ import annotations

import asyncio

import pytest
import structlog

from memex.api.auth import DEFAULT_DEV_USER_ID, current_user_id
from memex.config import settings
from memex.logging import clear_request_context


def test_current_user_id_binds_user_id_when_auth_disabled() -> None:
    async def _check() -> tuple[int, object]:
        clear_request_context()
        uid = await current_user_id(authorization=None)
        bound = structlog.contextvars.get_contextvars().get("user_id")
        return uid, bound

    uid, bound_uid = asyncio.run(_check())
    assert uid == DEFAULT_DEV_USER_ID
    assert bound_uid == DEFAULT_DEV_USER_ID


def test_current_user_id_binds_user_id_with_valid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "api_token", "secret-test")

    async def _check() -> tuple[int, object]:
        clear_request_context()
        uid = await current_user_id(authorization="Bearer secret-test")
        bound = structlog.contextvars.get_contextvars().get("user_id")
        return uid, bound

    uid, bound_uid = asyncio.run(_check())
    assert uid == DEFAULT_DEV_USER_ID
    assert bound_uid == DEFAULT_DEV_USER_ID


def test_current_user_id_does_not_bind_on_rejected_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException

    monkeypatch.setattr(settings, "auth_enforced", True)
    monkeypatch.setattr(settings, "api_token", "secret-test")

    async def _check() -> object:
        clear_request_context()
        try:
            await current_user_id(authorization=None)
        except HTTPException as exc:
            return exc.status_code, structlog.contextvars.get_contextvars()
        return None

    result = asyncio.run(_check())
    assert result is not None
    status_code, bound = result
    assert status_code == 401
    assert "user_id" not in bound
