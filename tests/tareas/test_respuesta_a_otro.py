"""Principio 3 en el extractor, determinista: una tarea que sale SOLO de un mensaje que responde
a OTRA persona es tarea de esa persona, no de quien lee. Responder al propio usuario, o hablarle
al grupo («todos»), sí cuenta; sin el original en la tabla no se asume nada."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from memex.db import connection
from memex.modules.tareas.extract import _addressed_to_someone_else
from memex.modules.tareas.schema import TareaItem
from tests.tareas.test_extract import _persist, _rows, _source

CHAT = "-1001234567890"


def _msg(
    src: int,
    *,
    message_id: int,
    text_: str,
    sender: dict[str, Any],
    reply_to: int | None = None,
    when: str = "2026-05-29 16:53:24+00",
) -> int:
    payload: dict[str, Any] = {
        "chat_id": CHAT,
        "chat_title": "Programa",
        "message_id": message_id,
        "text": text_,
        "sender": sender,
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :s, :e, CAST(:o AS TIMESTAMPTZ), CAST(:p AS JSONB)) RETURNING id"
                ),
                {"s": src, "e": uuid4().hex, "o": when, "p": json.dumps(payload)},
            ).scalar_one()
        )


ORG = {"user_id": 11, "display_name": "Adriana | FIB", "is_self": False}
OTRO = {"user_id": 5, "display_name": "Tuti", "is_self": False}
YO = {"user_id": 1, "display_name": "Danilo", "is_self": True}


def test_regla_determinista() -> None:
    src = _source("telegram", mode="programa")
    base = uuid4().int % 1_000_000
    otro = _msg(src, message_id=base + 1, text_="Yo!", sender=OTRO)
    a_otro = _msg(
        src, message_id=base + 2, text_="Confírmame nombre y país", sender=ORG, reply_to=base + 1
    )
    _msg(src, message_id=base + 3, text_="¿Qué pongo acá?", sender=YO)
    a_mi = _msg(src, message_id=base + 4, text_="Pon que no", sender=ORG, reply_to=base + 3)
    a_todos = _msg(
        src,
        message_id=base + 5,
        text_="Todos confirmen nombre y país",
        sender=ORG,
        reply_to=base + 1,
    )
    sin_original = _msg(
        src, message_id=base + 6, text_="Confírmame", sender=ORG, reply_to=999_999_999
    )
    suelto = _msg(src, message_id=base + 7, text_="Suban el ejercicio", sender=ORG)
    with connection() as c:
        assert _addressed_to_someone_else(c, [a_otro])
        assert not _addressed_to_someone_else(c, [a_mi])
        assert not _addressed_to_someone_else(c, [a_todos])
        assert not _addressed_to_someone_else(c, [sin_original])
        assert not _addressed_to_someone_else(c, [suelto])
        assert not _addressed_to_someone_else(c, [a_otro, suelto])  # otra fuente la sostiene
        assert not _addressed_to_someone_else(c, [])
    assert otro  # el original existe en la tabla (fuera de cualquier lote)


def test_la_respuesta_a_otra_persona_no_nace_como_tarea_propia() -> None:
    src = _source("telegram", mode="programa")
    base = uuid4().int % 1_000_000
    _msg(src, message_id=base + 1, text_="Yo!", sender=OTRO)
    a_otro = _msg(
        src, message_id=base + 2, text_="Confírmame nombre y país", sender=ORG, reply_to=base + 1
    )
    mio = _msg(src, message_id=base + 3, text_="¿Qué pongo acá?", sender=YO)
    a_mi = _msg(src, message_id=base + 4, text_="Pon que no", sender=ORG, reply_to=base + 3)
    _persist(
        (a_otro, a_mi),
        [
            TareaItem(source_inbox_ids=(a_otro,), title="Confirmar nombre y país"),
            TareaItem(source_inbox_ids=(a_mi,), title="Marcar que no en el formulario"),
        ],
    )
    assert _rows(a_otro) == []
    assert [r["title"] for r in _rows(a_mi)] == ["Marcar que no en el formulario"]
    assert mio
