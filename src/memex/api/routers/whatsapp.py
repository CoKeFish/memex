"""Vincular WhatsApp y elegir qué chats entran (la vía UI de /cuenta).

`POST /pair` pide el código de «Vincular con número de teléfono» y lo devuelve; el dueño lo escribe
en el teléfono y el cliente vivo (`runtime`) recibe el vínculo. `GET /status` es lo que la UI
consulta mientras espera. `GET /chats` lista el catálogo que la sesión fue viendo (historial
inicial + mensajes en vivo) con su estado de inclusión; `POST /chats` guarda la selección en
`sources.config.allowed_chats` (formato Telegram) y vuelca la bandeja de los chats recién incluidos
al inbox. `POST /unlink` cierra la sesión y borra la base del dispositivo.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from memex.api.auth import current_user_id
from memex.db import connection
from memex.ingestors.whatsapp.config import WaAllowedChat
from memex.logging import get_logger
from memex.whatsapp import store
from memex.whatsapp.runtime import runtime

router = APIRouter(prefix="/accounts/{account_id}/whatsapp", tags=["whatsapp"])
UserID = Annotated[int, Depends(current_user_id)]
_log = get_logger("memex.api.whatsapp")

_PHONE_RE = re.compile(r"^\d{8,15}$")


class PairRequest(BaseModel):
    phone: str = Field(description="Número con indicativo, sin '+': 57300XXXXXXX")


class ChatSelection(BaseModel):
    chat_id: str = Field(min_length=3)
    name: str = ""
    mode: Literal["anuncios", "programa", "intimo"] = "anuncios"
    since: date | None = None


class ChatsPut(BaseModel):
    allowed: list[ChatSelection]


def _assert_owns(conn: Any, user_id: int, account_id: int) -> None:
    row = (
        conn.execute(
            text("SELECT user_id, provider FROM accounts WHERE id = :aid"), {"aid": account_id}
        )
        .mappings()
        .first()
    )
    if row is None or int(row["user_id"]) != user_id:
        raise HTTPException(status_code=404, detail="account not found")
    if str(row["provider"]) != "whatsapp":
        raise HTTPException(status_code=422, detail="la cuenta no es de WhatsApp")


def _mask(phone: str) -> str:
    return f"…{phone[-4:]}" if len(phone) >= 4 else phone


def _status_payload(conn: Any, account_id: int) -> dict[str, Any]:
    link = store.get_link(conn, account_id)
    status = str(link["status"]) if link else "sin_vincular"
    chats = int(
        conn.execute(
            text("SELECT count(*) FROM whatsapp_chats WHERE account_id = :aid"), {"aid": account_id}
        ).scalar_one()
    )
    return {
        "status": status,
        "connected": runtime.is_connected(account_id),
        "phone_masked": _mask(str(link["phone"])) if link and link["phone"] else "",
        "jid": (link or {}).get("jid"),
        "pair_code": (link or {}).get("pair_code") if status == "codigo" else None,
        "linked_at": (link or {}).get("linked_at"),
        "last_event_at": (link or {}).get("last_event_at"),
        "last_error": (link or {}).get("last_error"),
        "chats": chats,
        "buffered": store.buffer_count(conn, account_id),
    }


@router.get("/status")
async def status(account_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns(conn, user_id, account_id)
        return _status_payload(conn, account_id)


@router.post("/pair")
async def pair(account_id: int, body: PairRequest, user_id: UserID) -> dict[str, Any]:
    """Pide el código de vinculación para el número. Devuelve el código (dura pocos minutos)."""
    phone = re.sub(r"[\s+\-()]", "", body.phone)
    if not _PHONE_RE.match(phone):
        raise HTTPException(
            status_code=422, detail="número inválido: indicativo + número, solo dígitos"
        )
    with connection() as conn:
        _assert_owns(conn, user_id, account_id)
        link = store.get_link(conn, account_id)
        if link and str(link["status"]) == "vinculado" and runtime.is_connected(account_id):
            raise HTTPException(status_code=409, detail="esta cuenta ya está vinculada")
    try:
        code = await runtime.pair(user_id, account_id, phone)
    except Exception as e:
        _log.warning("whatsapp.pair.failed", account_id=account_id, error=str(e))
        raise HTTPException(status_code=502, detail=f"no se pudo pedir el código: {e}") from e
    return {"status": "codigo", "code": code, "phone_masked": _mask(phone)}


@router.get("/chats")
async def list_chats(account_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns(conn, user_id, account_id)
        src = store.ensure_source(conn, user_id, account_id)
        allowed = store.load_allowed(conn, int(src["id"]))
        chats = store.list_chats(conn, account_id, allowed)
    return {"source_id": int(src["id"]), "chats": chats}


@router.post("/chats")
async def put_chats(account_id: int, body: ChatsPut, user_id: UserID) -> dict[str, Any]:
    """Guarda la selección y vuelca la bandeja de los chats que pasaron a estar incluidos."""
    chats = [
        WaAllowedChat(chat_id=c.chat_id, name=c.name, mode=c.mode, since=c.since)
        for c in body.allowed
    ]
    with connection() as conn:
        _assert_owns(conn, user_id, account_id)
        src = store.ensure_source(conn, user_id, account_id)
        before = set(store.load_allowed(conn, int(src["id"])))
        store.save_allowed(conn, int(src["id"]), chats)
    newly = sorted({c.chat_id for c in chats} - before)
    await runtime.reload_allowed(account_id)
    ingested = await runtime.drain(user_id, account_id, newly)
    _log.info(
        "whatsapp.chats.saved",
        account_id=account_id,
        allowed=len(chats),
        newly_included=len(newly),
        ingested=ingested,
    )
    return {"saved": len(chats), "newly_included": newly, "ingested": ingested}


@router.post("/unlink")
async def unlink(account_id: int, user_id: UserID) -> dict[str, Any]:
    with connection() as conn:
        _assert_owns(conn, user_id, account_id)
    await runtime.unlink(user_id, account_id)
    return {"status": "cerrado"}
