"""Principio «la señal pertenece al hilo» en el asignador de asuntos: un mensaje de chat que
responde a otro lleva la cita del original («↩ en respuesta a quién: «texto»»), esté el original en
el lote o en otra ventana. Sin eso «no quiero hacerlo» llegaba sin hilo al modelo."""

from __future__ import annotations

from sqlalchemy import Connection

from memex.modules.asuntos.adhesion import load_messages
from memex.modules.asuntos.assign import render_batch
from tests.test_chat_context import CHAT, _msg, _source


def test_reply_quote_dentro_del_lote(conn: Connection) -> None:
    sid = _source(conn, [{"chat_id": CHAT, "mode": "intimo"}])
    original = _msg(conn, sid, 1, name="Ana", text_="¿Cómo les fue con sus videos de la beca?")
    respuesta = _msg(
        conn, sid, 2, user_id=1, name="Yo", text_="No quiero hacerlo", is_self=True, reply_to=1
    )
    views = load_messages(conn, 1, [original, respuesta], body_chars=200)
    assert views[0].reply_quote == ""
    assert views[1].reply_quote == "Ana: «¿Cómo les fue con sus videos de la beca?»"
    rendered = render_batch(views)
    assert "↩ en respuesta a Ana: «¿Cómo les fue con sus videos de la beca?»" in rendered


def test_reply_quote_busca_el_original_fuera_del_lote(conn: Connection) -> None:
    sid = _source(conn, [{"chat_id": CHAT, "mode": "intimo"}])
    _msg(conn, sid, 10, user_id=1, name="Yo", text_="Mañana grabo el video", is_self=True)
    respuesta = _msg(conn, sid, 11, name="Ana", text_="¡Dale!", reply_to=10)
    views = load_messages(conn, 1, [respuesta], body_chars=200)
    assert views[0].reply_quote == "el usuario: «Mañana grabo el video»"


def test_sin_original_no_hay_cita(conn: Connection) -> None:
    sid = _source(conn, [{"chat_id": CHAT, "mode": "intimo"}])
    huerfana = _msg(conn, sid, 20, name="Ana", text_="ok", reply_to=999)
    views = load_messages(conn, 1, [huerfana], body_chars=200)
    assert views[0].reply_quote == "" and views[0].is_reply is False
