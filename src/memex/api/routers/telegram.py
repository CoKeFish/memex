"""Login multi-paso de Telegram + discover de chats por HTTP (la vía UI del dashboard).

Telegram no tiene OAuth web: el login manda un código al teléfono que hay que ingresar (+ 2FA
opcional). Se parte en 2 requests (`request-code` → `submit-code` [→ `submit-password`]); entre
ellos se mantiene VIVO el `TelegramClient` en memoria, keyed por un token opaco (mismo patrón que
`_PENDING_VERIFIERS` del OAuth de Google). La sesión autorizada queda en el archivo que lee el
ingestor (`MEMEX_TG_SESSION_PATH`). Las credenciales (api_id/api_hash/phone) salen del VAULT.

Equivalente por CLI: `memex-telegram auth` (interactivo). El `/chats` (discover) espeja
`memex-telegram discover`; la selección se persiste vía `PATCH /sources/{id}` (allowed_chats).

Estado en memoria, 1 worker: se pierde al reiniciar el API (el flujo en curso se reintenta). NO
concurrente con el ingestor sobre el MISMO session file (durante el primer login no hay sesión, así
que el streaming runner aún no la usa).
"""

from __future__ import annotations

import contextlib
import secrets
import time
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from memex.api.auth import current_user_id
from memex.core.source import SourceConfigError
from memex.db import connection
from memex.ingestors.telegram.client import TelegramAuthError, TelegramClientWrapper
from memex.ingestors.telegram.config import TelegramConfig, TelegramConfigError
from memex.logging import get_logger
from memex.sources.resolver import build_resolved_env

router = APIRouter(prefix="/accounts/{account_id}/telegram", tags=["telegram"])

UserID = Annotated[int, Depends(current_user_id)]

_log = get_logger("memex.api.telegram")

#: TTL del flujo de login en vuelo. Pasado esto, el client se desconecta y el usuario reintenta.
_AUTH_TTL = 600.0


@dataclass
class _PendingAuth:
    """Flujo de login en curso: el `TelegramClient` VIVO entre request-code y submit-code."""

    client: TelegramClient
    phone: str
    phone_code_hash: str
    account_id: int
    user_id: int
    expires_at: float


#: token opaco → flujo en vuelo. En memoria (1 worker). El token viaja al cliente como `state`.
_PENDING: dict[str, _PendingAuth] = {}


class SubmitCodeRequest(BaseModel):
    state: str
    code: str


class SubmitPasswordRequest(BaseModel):
    state: str
    password: str


async def _sweep_expired() -> None:
    """Desconecta y descarta flujos vencidos (no dejar sockets colgados)."""
    now = time.time()
    for token in [t for t, p in _PENDING.items() if p.expires_at < now]:
        pending = _PENDING.pop(token, None)
        if pending is not None:
            with contextlib.suppress(Exception):
                await pending.client.disconnect()


def _assert_owns(conn: Any, user_id: int, account_id: int) -> None:
    owner = conn.execute(
        text("SELECT user_id FROM accounts WHERE id = :aid"), {"aid": account_id}
    ).scalar()
    if owner != user_id:
        raise HTTPException(status_code=404, detail="account not found")


def _resolve_tg_config(conn: Any, user_id: int, account_id: int) -> TelegramConfig:
    """Config Telegram de la source vinculada a la cuenta, con credenciales del VAULT."""
    src = (
        conn.execute(
            text(
                "SELECT config FROM sources WHERE account_id = :aid AND type = 'telegram' "
                "ORDER BY id LIMIT 1"
            ),
            {"aid": account_id},
        )
        .mappings()
        .first()
    )
    if src is None:
        raise HTTPException(
            status_code=422,
            detail="vinculá una source de Telegram a la cuenta antes de conectar",
        )
    cfg = dict(src["config"] or {})
    env = build_resolved_env(
        conn, user_id=user_id, source_type="telegram", cfg=cfg, account_id=account_id
    )
    try:
        return TelegramConfig.from_source_config(cfg, env)
    except (TelegramConfigError, SourceConfigError) as e:
        raise HTTPException(
            status_code=422, detail=f"config/credenciales de Telegram inválidas: {e}"
        ) from e


def _get_pending(state: str, user_id: int, account_id: int) -> _PendingAuth:
    pending = _PENDING.get(state)
    if pending is None or pending.expires_at < time.time():
        raise HTTPException(status_code=410, detail="login expirado; reiniciá pidiendo un código")
    if pending.user_id != user_id or pending.account_id != account_id:
        raise HTTPException(status_code=404, detail="login no encontrado")
    return pending


async def _finalize(state: str, account_id: int) -> dict[str, Any]:
    """Cierra el flujo: persiste la sesión (la escribe Telethon en el archivo), desconecta, marca
    la cuenta healthy."""
    pending = _PENDING.pop(state, None)
    if pending is not None:
        with contextlib.suppress(Exception):
            pending.client.session.save()
        with contextlib.suppress(Exception):
            await pending.client.disconnect()
    with connection() as conn:
        conn.execute(
            text(
                "UPDATE accounts SET health_status = 'healthy', last_health_check_at = NOW() "
                "WHERE id = :aid"
            ),
            {"aid": account_id},
        )
    _log.info("telegram.auth.ok", account_id=account_id)
    return {"status": "ok", "detail": "Telegram conectado"}


@router.post("/request-code")
async def request_code(account_id: int, user_id: UserID) -> dict[str, Any]:
    """Paso 1: manda el código al teléfono de la cuenta (credenciales del vault)."""
    await _sweep_expired()
    with connection() as conn:
        _assert_owns(conn, user_id, account_id)
        cfg = _resolve_tg_config(conn, user_id, account_id)

    cfg.session_path.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(cfg.session_file), cfg.api_id, cfg.api_hash)
    await client.connect()
    if await client.is_user_authorized():
        await client.disconnect()
        return {"status": "already_authorized", "phone_masked": cfg.phone_masked}
    try:
        sent = await client.send_code_request(cfg.phone)
    except FloodWaitError as e:
        await client.disconnect()
        raise HTTPException(
            status_code=429, detail=f"demasiados intentos; esperá {e.seconds}s"
        ) from e
    except Exception as e:
        await client.disconnect()
        _log.error("telegram.auth.send_code_failed", account_id=account_id, exc_msg=str(e))
        raise HTTPException(status_code=502, detail=f"no se pudo enviar el código: {e}") from e

    token = secrets.token_urlsafe(32)
    _PENDING[token] = _PendingAuth(
        client=client,
        phone=cfg.phone,
        phone_code_hash=sent.phone_code_hash,
        account_id=account_id,
        user_id=user_id,
        expires_at=time.time() + _AUTH_TTL,
    )
    _log.info("telegram.auth.code_requested", account_id=account_id)
    return {"status": "code_sent", "state": token, "phone_masked": cfg.phone_masked}


@router.post("/submit-code")
async def submit_code(account_id: int, body: SubmitCodeRequest, user_id: UserID) -> dict[str, Any]:
    """Paso 2: envía el código. Si hay 2FA, devuelve `2fa_required` (el flujo sigue vivo)."""
    pending = _get_pending(body.state, user_id, account_id)
    try:
        await pending.client.sign_in(
            pending.phone, body.code, phone_code_hash=pending.phone_code_hash
        )
    except SessionPasswordNeededError:
        return {
            "status": "2fa_required",
            "detail": "ingresá tu contraseña de verificación en 2 pasos",
        }
    except PhoneCodeInvalidError as e:
        raise HTTPException(status_code=400, detail="código inválido") from e
    except PhoneCodeExpiredError as e:
        await _sweep_drop(body.state)
        raise HTTPException(status_code=410, detail="código expirado; pedí uno nuevo") from e
    except Exception as e:
        await _sweep_drop(body.state)
        _log.error("telegram.auth.sign_in_failed", account_id=account_id, exc_msg=str(e))
        raise HTTPException(status_code=502, detail=f"login falló: {e}") from e
    return await _finalize(body.state, account_id)


@router.post("/submit-password")
async def submit_password(
    account_id: int, body: SubmitPasswordRequest, user_id: UserID
) -> dict[str, Any]:
    """Paso 2b (2FA): envía la contraseña de verificación en dos pasos."""
    pending = _get_pending(body.state, user_id, account_id)
    try:
        await pending.client.sign_in(password=body.password)
    except Exception as e:
        _log.error("telegram.auth.2fa_failed", account_id=account_id, exc_msg=str(e))
        raise HTTPException(status_code=400, detail=f"contraseña 2FA inválida: {e}") from e
    return await _finalize(body.state, account_id)


async def _sweep_drop(state: str) -> None:
    """Descarta un flujo (desconecta su client) tras un fallo terminal."""
    pending = _PENDING.pop(state, None)
    if pending is not None:
        with contextlib.suppress(Exception):
            await pending.client.disconnect()


@router.get("/chats")
async def list_chats(account_id: int, user_id: UserID) -> dict[str, Any]:
    """Discover: lista los grupos/canales accesibles (necesita sesión autorizada = login hecho)."""
    with connection() as conn:
        _assert_owns(conn, user_id, account_id)
        cfg = _resolve_tg_config(conn, user_id, account_id)

    chats: list[dict[str, Any]] = []
    try:
        async with TelegramClientWrapper(cfg) as tc:
            async for d in tc.iter_dialogs():
                kind = "channel" if d.is_channel else ("group" if d.is_group else "user")
                chats.append({"chat_id": int(d.id), "name": str(d.name or ""), "kind": kind})
    except TelegramAuthError as e:
        raise HTTPException(
            status_code=422, detail="Telegram no está conectado; hacé el login primero"
        ) from e
    _log.info("telegram.discover.ok", account_id=account_id, count=len(chats))
    return {"chats": chats}
