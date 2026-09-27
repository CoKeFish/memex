"""Una propuesta de chat sin confirmar no es evento (2026-09-26): «les propongo que hagamos X el
26, ¿quién se apunta?» no inserta nada aunque el extractor lo devuelva como anuncio; lo
confirmado (la persona dice que va), lo fijado por quien organiza («confirmado», «queda para
el…») y lo que viene por correo sí entran."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text

from memex.db import connection
from memex.llm import LLMClient
from memex.modules.calendar.module import CalendarModule
from memex.modules.calendar.prompt import CALENDAR_SYSTEM_PROMPT
from memex.modules.calendar.propuestas import sounds_like_proposal
from memex.modules.calendar.schema import CalendarEventItem
from memex.modules.contract import ModuleContext

PROPUESTA = (
    "hola hola feliz domingo! Les propongo que hagamos Cripto Tertulia la otra semana y otro en "
    "quince dias (26 de set y 10 de oct)... quiero ver quien se apunta para confirmar fechas..."
)


def _source(stype: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
                {"n": f"{stype}-{uuid4().hex[:6]}", "t": stype},
            ).scalar_one()
        )


def _inbox(src: int, payload: dict[str, Any]) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, NOW(), CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": src, "e": uuid4().hex, "p": json.dumps(payload)},
            ).scalar_one()
        )


def _chat(src: int, text_: str, *, is_self: bool = False) -> int:
    return _inbox(
        src,
        {
            "chat_id": "-100123",
            "chat_title": "Grupo",
            "message_id": uuid4().int % 1_000_000,
            "text": text_,
            "sender": {"user_id": 1 if is_self else 7, "display_name": "K", "is_self": is_self},
        },
    )


def _persist(inbox_ids: tuple[int, ...], items: list[CalendarEventItem]) -> int:
    async def run() -> int:
        with connection() as conn:
            ctx = ModuleContext(
                user_id=1,
                conn=conn,
                llm=cast(LLMClient, None),
                deps={},
                summary_id=None,
                inbox_ids=inbox_ids,
            )
            return await CalendarModule().persist(ctx, items)

    return asyncio.run(run())


def _events_of(inbox_id: int) -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT id, title, commitment FROM mod_calendar_events "
                    "WHERE user_id = 1 AND :i = ANY(source_inbox_ids) ORDER BY id"
                ),
                {"i": inbox_id},
            ).mappings()
        ]


def _item(iid: int, title: str, commitment: str, evidence: str) -> CalendarEventItem:
    return CalendarEventItem(
        source_inbox_ids=(iid,),
        title=title,
        starts_on=date(2026, 9, 26),
        commitment=commitment,
        commitment_evidence=evidence[:80],
        evidence=evidence,
    )


def test_sounds_like_proposal() -> None:
    assert sounds_like_proposal(PROPUESTA)
    assert sounds_like_proposal("Qué tal si nos vemos el jueves?")
    assert sounds_like_proposal("¿Quiénes se apuntan al café del 26?")
    assert not sounds_like_proposal("Confirmado: Cripto Tertulia el 26 a las 6 pm por Meet")
    assert not sounds_like_proposal("Les propongo… bueno, ya quedó para el 26 a las 6")
    assert not sounds_like_proposal("Hackathon Bogotá 2026: 29 de agosto, inscripciones abiertas")
    assert not sounds_like_proposal("")


def test_la_propuesta_de_chat_no_entra_ni_como_anuncio() -> None:
    src = _source("telegram")
    m = _chat(src, PROPUESTA)
    n = _persist((m,), [_item(m, "Cripto Tertulia", "anuncio", PROPUESTA[:120])])
    assert n == 0 and _events_of(m) == []
    # provisional (le hablan como participante) pero sigue siendo una propuesta: tampoco
    m2 = _chat(src, "¿Qué tal si hacemos el café el 26? ¿Te apuntás?")
    assert _persist((m2,), [_item(m2, "Café", "provisional", "¿Qué tal si hacemos")]) == 0


def test_lo_fijado_lo_confirmado_y_el_correo_si_entran() -> None:
    src = _source("telegram")
    fijado = _chat(
        src, "Confirmado: Cripto Tertulia el 26 de septiembre a las 6 pm, queda para ese día"
    )
    assert (
        _persist((fijado,), [_item(fijado, "Cripto Tertulia", "anuncio", "Confirmado: Cripto")])
        == 1
    )
    yo = _chat(src, "Les propongo que hagamos el café el 26, yo voy sí o sí", is_self=True)
    assert _persist((yo,), [_item(yo, "Café", "confirmado", "yo voy sí o sí")]) == 1
    mail = _source("imap")
    corre = _inbox(mail, {"subject": "Propuesta de fecha", "body_text": PROPUESTA})
    assert _persist((corre,), [_item(corre, "Cripto Tertulia", "anuncio", PROPUESTA[:120])]) == 1


def test_el_prompt_lo_dice() -> None:
    assert "UNA PROPUESTA NO ES UN EVENTO" in CALENDAR_SYSTEM_PROMPT
