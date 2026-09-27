"""Render del lote para el LLM (`orchestrator._build_messages`): en chats, una ráfaga del mismo
remitente va como UN item con `ids`, y un mensaje que responde a otro del lote lleva «↩ en
respuesta a X: …» al frente. Sin esto el extractor de tareas veía «Por 70 puntos:» solo, y no
sabía que «confírmame nombre y país» era para otra persona (2026-09-20).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from memex.modules.orchestrator import _build_messages
from memex.processing.windows import Window, WorkRow

_T0 = datetime(2026, 5, 8, 0, 40, 28, tzinfo=UTC)


def _row(
    inbox_id: int,
    text: str,
    *,
    at: datetime,
    sender: str = "Adriana | FIB",
    user_id: int = 7,
    message_id: int | None = None,
    reply_to: int | None = None,
    topic: int | None = 20252,
    chat: str = "-1001234567890",
) -> WorkRow:
    payload: dict[str, Any] = {
        "chat_id": chat,
        "chat_title": "Grupo",
        "topic_id": topic,
        "message_id": message_id if message_id is not None else inbox_id,
        "text": text,
        "sender": {"user_id": user_id, "display_name": sender, "is_self": False},
    }
    if reply_to is not None:
        payload["reply_to_message_id"] = reply_to
    return WorkRow(
        inbox_id=inbox_id,
        source_id=3,
        occurred_at=at,
        payload=payload,
        tier="chat",
        source_type="telegram",
    )


def _items(rows: list[WorkRow]) -> list[dict[str, Any]]:
    raw, _ = _build_messages(Window(tier="chat", source_id=3, rows=tuple(rows)))
    out: list[dict[str, Any]] = json.loads(raw)
    return out


def test_rafaga_del_mismo_remitente_va_como_un_item_con_ids() -> None:
    rows = [
        _row(1, "Por 70 puntos:", at=_T0),
        _row(2, "🐘🦁 🐕🐭🧲", at=_T0 + timedelta(seconds=1)),
        _row(3, "El que lo resuelva pone lo que pide", at=_T0 + timedelta(seconds=50)),
        _row(4, "Solo el primero se lleva los puntos", at=_T0 + timedelta(seconds=58)),
        _row(5, "yo intento", at=_T0 + timedelta(seconds=70), sender="Karu", user_id=9),
    ]
    items = _items(rows)
    assert [it["id"] for it in items] == [1, 5]
    assert items[0]["ids"] == [1, 2, 3, 4]
    assert "Por 70 puntos:" in items[0]["text"] and "Solo el primero" in items[0]["text"]
    assert "ids" not in items[1]


def test_rafaga_se_corta_por_tiempo_tema_o_respuesta() -> None:
    lejos = [_row(1, "a", at=_T0), _row(2, "b", at=_T0 + timedelta(minutes=5))]
    assert [it["id"] for it in _items(lejos)] == [1, 2]
    otro_tema = [_row(1, "a", at=_T0), _row(2, "b", at=_T0 + timedelta(seconds=5), topic=1)]
    assert [it["id"] for it in _items(otro_tema)] == [1, 2]
    respuesta = [_row(1, "a", at=_T0), _row(2, "b", at=_T0 + timedelta(seconds=5), reply_to=1)]
    assert [it["id"] for it in _items(respuesta)] == [1, 2]


def test_respuesta_a_otro_mensaje_del_lote_lleva_la_cita_al_frente() -> None:
    rows = [
        _row(1, "¿Puedo entrar al grupo de Colombia?", at=_T0, sender="Tuti", user_id=5),
        _row(2, "Confírmame nombre, país y estrategia", at=_T0 + timedelta(minutes=3), reply_to=1),
    ]
    items = _items(rows)
    assert items[1]["text"].startswith(
        "↩ en respuesta a Tuti: «¿Puedo entrar al grupo de Colombia?»"
    )
    assert items[1]["text"].endswith("Confírmame nombre, país y estrategia")
    # respuesta a un mensaje que NO está en el lote ni en la tabla: sin cita, sin romper
    solo = _items([_row(2, "ok", at=_T0, reply_to=999)])
    assert solo[0]["text"].endswith("ok") and "↩" not in solo[0]["text"]


def test_respuesta_a_un_mensaje_de_otra_ventana_busca_el_original_en_la_tabla() -> None:
    """El original está en otra ventana (o el lote se armó a mano, como un reproceso por ids):
    la cita igual va al frente, así el extractor sabe que «confírmame nombre» era para otra
    persona y no una tarea propia (2026-09-26)."""
    from sqlalchemy import text as sql

    from memex.db import connection

    chat = "-100987654321"
    with connection() as c:
        sid = int(
            c.execute(
                sql(
                    "INSERT INTO sources (user_id, name, type) "
                    "VALUES (1, 'tg-render-test', 'telegram') RETURNING id"
                )
            ).scalar_one()
        )
        c.execute(
            sql(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :s, 'orig-1', :o, CAST(:p AS JSONB))"
            ),
            {
                "s": sid,
                "o": _T0,
                "p": json.dumps(
                    {
                        "chat_id": chat,
                        "message_id": 41,
                        "text": "Yo!",
                        "sender": {"user_id": 5, "display_name": "Tuti", "is_self": False},
                    }
                ),
            },
        )
    from dataclasses import replace

    reply = replace(
        _row(2, "Confírmame nombre, país y estrategia", at=_T0, reply_to=41, chat=chat),
        source_id=sid,
    )
    items = _items([reply])
    assert items[0]["text"].startswith("↩ en respuesta a Tuti: «Yo!»")


def test_correos_y_mensajes_sin_chat_no_se_agrupan() -> None:
    mail = {"subject": "Hola", "from": {"name": "A", "email": "a@x.com"}, "body_text": "cuerpo"}
    rows = [
        WorkRow(inbox_id=1, source_id=1, occurred_at=_T0, payload=mail, tier="email"),
        WorkRow(inbox_id=2, source_id=1, occurred_at=_T0, payload=mail, tier="email"),
    ]
    items = _items(rows)
    assert [it["id"] for it in items] == [1, 2] and all("ids" not in it for it in items)
