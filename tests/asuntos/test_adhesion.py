from __future__ import annotations

from typing import Any

from memex.db import connection
from memex.modules.asuntos import store
from memex.modules.asuntos.adhesion import (
    adhere,
    clean_body,
    event_key,
    load_messages,
    normalize_subject,
)
from tests.asuntos._seed import asunto, msg, source


def test_clean_body_quita_relleno_invisible_y_urls_de_rastreo_antes_del_recorte() -> None:
    # Boletín real (beehiiv por el plugin de Outlook, 2026-09-15): preheader + 200 caracteres
    # invisibles + pixel de rastreo de 400 caracteres ANTES del contenido. Con el recorte primero,
    # el modelo nunca leía la frase del cambio de nombre.
    padding = (chr(0x200C) + " ") * 200  # ZWNJ: se escribe así para que se vea en el código
    pixel = "<https://link.mail.beehiiv.com/v2/o/" + "a" * 400 + ">"
    raw = (
        f"De aprender a contribuir. {padding} September 15 {pixel} ¡Hola! El espacio que conocías "
        "como StarForge pasó a ser el Programa de Embajadores de Sideral. "
        "Llamada: https://meet.google.com/sxc-qchy-nrf"
    )
    b = clean_body(raw, 300)
    assert chr(0x200C) not in b
    assert "link.mail.beehiiv.com/…" in b and "aaaa" not in b
    assert "StarForge pasó a ser" in b
    # las URLs cortas (un Meet) se conservan enteras: pueden ser el dato que importa
    assert "https://meet.google.com/sxc-qchy-nrf" in b


def test_normalize_subject_quita_prefijos_numeros_y_sesiones() -> None:
    assert normalize_subject(
        "RE: Fwd: Impulsa Bootcamp · Satélite Bogotá — S01"
    ) == normalize_subject("Impulsa Bootcamp Satélite Bogotá S03")
    assert normalize_subject("Blockville – Sideral Summer Friday v17.0") == normalize_subject(  # noqa: RUF001
        "Blockville – Sideral Summer Friday v18.0"  # noqa: RUF001
    )


def test_event_key_luma_y_calendar() -> None:
    k = event_key("Registration confirmed for Impact Studio: Sideral Ecosystem Dinner")
    assert k and k == event_key("⏰ Impact Studio: Sideral Ecosystem Dinner is starting tomorrow")
    assert event_key("Update for Impact Studio: Sideral Ecosystem Dinner") == k
    g = event_key(
        "Invitación: Sprint de selección Alebrije (Masterclass 1) lun 23 de feb de 2026 6:30pm"
    )
    assert g and g == event_key(
        "Invitación actualizada: Sprint de selección Alebrije (Masterclass 1) @ 8pm"
    )
    assert event_key("Hola") is None


def test_adhesion_por_hilo_evento_y_asunto(conn: Any) -> None:
    src = source()
    aid = asunto("Impact Studio", status="abierto")
    m0 = msg(
        src,
        subject="Registration confirmed for Impact Studio Dinner",
        from_addr="x@calendar.luma-mail.com",
        message_id="m0@luma",
    )
    with connection() as c:
        [mv0] = load_messages(c, 1, [m0], body_chars=500)
        assert mv0.claim_keys  # hilo (message_id) + evento + asunto
        store.add_evidence(c, 1, aid, m0, method="llm")
        store.add_keys(c, 1, aid, mv0.claim_keys)
    reply = msg(
        src, subject="Re: cualquier cosa", from_addr="alguien@otro.com", in_reply_to="m0@luma"
    )
    luma = msg(
        src,
        subject="⏰ Impact Studio Dinner is starting in 1 hour",
        from_addr="y@calendar.luma-mail.com",
    )
    same = msg(
        src,
        subject="RE: Registration confirmed for Impact Studio Dinner",
        from_addr="z@calendar.luma-mail.com",
    )
    other = msg(src, subject="Newsletter semanal", from_addr="news@otro.com")
    with connection() as c:
        msgs = load_messages(c, 1, [reply, luma, same, other], body_chars=500)
        glued = adhere(c, 1, msgs)
    assert glued[reply] == (aid, "hilo")
    assert glued[luma] == (aid, "evento")
    assert glued[same][0] == aid and glued[same][1] in ("evento", "asunto")
    assert other not in glued


def test_adhesion_respeta_rechazo(conn: Any) -> None:
    src = source()
    aid = asunto("Evento", status="abierto")
    m0 = msg(
        src,
        subject="Registration confirmed for Gran Evento",
        from_addr="a@calendar.luma-mail.com",
    )
    with connection() as c:
        [mv0] = load_messages(c, 1, [m0], body_chars=500)
        store.add_evidence(c, 1, aid, m0, method="llm")
        store.add_keys(c, 1, aid, mv0.claim_keys)
    m1 = msg(src, subject="Update for Gran Evento", from_addr="b@calendar.luma-mail.com")
    with connection() as c:
        assert adhere(c, 1, load_messages(c, 1, [m1], body_chars=500))[m1] == (aid, "evento")
        store.add_evidence(c, 1, aid, m1, method="evento")  # lo que hace _adhere_batch
        [ev] = [e for e in store.evidence_of(c, 1, aid) if e.inbox_id == m1]
        store.reject_evidence(c, 1, ev.id)
        assert adhere(c, 1, load_messages(c, 1, [m1], body_chars=500)) == {}


def test_adhesion_no_pega_a_cerrados_ni_abre_asuntos(conn: Any) -> None:
    src = source()
    aid = asunto("Viejo", status="abierto")
    m0 = msg(src, subject="Confirmación de reserva Wingo BOG-MDE", from_addr="a@wingo.com")
    with connection() as c:
        [mv0] = load_messages(c, 1, [m0], body_chars=500)
        store.add_evidence(c, 1, aid, m0, method="llm")
        store.add_keys(c, 1, aid, mv0.claim_keys)
        store.record_state(c, 1, aid, "fin", closed=True)
    m1 = msg(src, subject="Confirmación de reserva Wingo BOG-MDE", from_addr="a@wingo.com")
    with connection() as c:
        glued = adhere(c, 1, load_messages(c, 1, [m1], body_chars=500))
    assert glued == {}
