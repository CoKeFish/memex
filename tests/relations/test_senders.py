"""Remitente→identidad en el grafo: la provenance DERIVADA (payload→identifiers) mete al remitente
resuelto en la co-ocurrencia de sus mensajes, y `ensure_chat_sender_identities` crea (una sola vez)
la identidad de los remitentes de CHAT desconocidos. Email/social solo RESUELVEN (nunca crean);
bots y mensajes de servicio quedan fuera."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.modules.identidades.chat_senders import weave_chat_structure
from memex.relations.cooccurrence import generate_cooccurrence
from memex.relations.edges import list_edges


def _exec(sql: str, **params: Any) -> Any:
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def _source(stype: str, name: str) -> int:
    return int(
        _exec(
            "INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id",
            n=name,
            t=stype,
        )
    )


def _inbox(source_id: int, ext: str, payload: dict[str, Any]) -> int:
    return int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :sid, :ext, NOW(), CAST(:p AS JSONB)) RETURNING id",
            sid=source_id,
            ext=ext,
            p=json.dumps(payload),
        )
    )


def _tg_payload(
    tg_id: int,
    *,
    username: str | None = None,
    display_name: str | None = None,
    is_bot: bool = False,
    chat_id: int = 900,
    text_: str = "hola",
) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "chat_kind": "group",
        "chat_title": "Parche",
        "sender": {
            "user_id": tg_id,
            "username": username,
            "display_name": display_name,
            "is_bot": is_bot,
        },
        "message_id": 1,
        "text": text_,
        "date": "2026-06-11T10:00:00Z",
    }


def _email_payload(email: str, name: str = "X") -> dict[str, Any]:
    return {"from": {"email": email, "name": name}, "folder": "Inbox", "subject": "s"}


def _finance(merchant: str, inbox_ids: list[int]) -> int:
    crudo = int(
        _exec(
            "INSERT INTO mod_finance_transactions "
            "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, counterparty) "
            "VALUES (1, :ids, 'egreso', 100, 'COP', NOW(), :m) RETURNING id",
            ids=inbox_ids,
            m=merchant,
        )
    )
    cons = int(
        _exec(
            "INSERT INTO mod_finance_consolidated (user_id, direction, amount, currency, "
            "occurred_at, counterparty) VALUES (1, 'egreso', 100, 'COP', NOW(), :m) RETURNING id",
            m=merchant,
        )
    )
    _exec(
        "INSERT INTO mod_finance_transaction_links (user_id, consolidated_id, transaction_id) "
        "VALUES (1, :c, :t)",
        c=cons,
        t=crudo,
    )
    return cons


def _person(name: str) -> int:
    return int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'persona', :n) RETURNING id",
            n=name,
        )
    )


def _identifier(identity_id: int, platform: str, kind: str, value: str) -> None:
    _exec(
        "INSERT INTO mod_identidades_identifiers "
        "(user_id, identity_id, platform, kind, value, value_norm) "
        "VALUES (1, :iid, :p, :k, :v, :v)",
        iid=identity_id,
        p=platform,
        k=kind,
        v=value,
    )


def _identities() -> list[tuple[int, str]]:
    with connection() as c:
        return [
            (int(r.id), str(r.display_name))
            for r in c.execute(
                text("SELECT id, display_name FROM mod_identidades WHERE user_id = 1 ORDER BY id")
            ).all()
        ]


def _identifiers_of(identity_id: int) -> set[tuple[str, str, str]]:
    with connection() as c:
        return {
            (str(r.platform), str(r.kind), str(r.value_norm))
            for r in c.execute(
                text(
                    "SELECT platform, kind, value_norm FROM mod_identidades_identifiers "
                    "WHERE user_id = 1 AND identity_id = :i"
                ),
                {"i": identity_id},
            ).all()
        }


def _pair(e: Any) -> set[tuple[str, int]]:
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}


def test_chat_sender_desconocido_se_crea_idempotente() -> None:
    src = _source("telegram", "tg")
    m1 = _inbox(src, "m1", _tg_payload(111, username="Juanito", display_name="Juan Niebla"))
    with connection() as c:
        _, senders, _ = weave_chat_structure(c, 1, [m1])
    assert senders == 1
    ids = _identities()
    assert len(ids) == 1
    iid, display = ids[0]
    assert display == "Juan Niebla"
    assert _identifiers_of(iid) == {
        ("telegram", "platform_id", "111"),
        ("telegram", "handle", "juanito"),
    }
    with connection() as c:
        _, senders2, _ = weave_chat_structure(c, 1, [m1])  # re-correr: ya existe, no duplica
    assert senders2 == 0
    assert len(_identities()) == 1


def test_bot_y_service_message_se_saltan() -> None:
    src = _source("telegram", "tg")
    m1 = _inbox(src, "m1", _tg_payload(500, username="robobot", display_name="Robo", is_bot=True))
    payload_service = _tg_payload(1)
    payload_service["sender"] = None  # mensaje de servicio / broadcast anónimo
    m2 = _inbox(src, "m2", payload_service)
    with connection() as c:
        _, senders, participa = weave_chat_structure(c, 1, [m1, m2])
    assert senders == 0
    assert participa == 0
    assert _identities() == []
    with connection() as c:
        assert list_edges(c, 1) == []


def test_enriquecimiento_por_username_no_crea() -> None:
    # el username ya era identifier de una identidad → se le ata el platform_id (no nace otra).
    p = _person("Juan Niebla")
    _identifier(p, "telegram", "handle", "juanito")
    src = _source("telegram", "tg")
    m1 = _inbox(src, "m1", _tg_payload(111, username="juanito", display_name="Juan N"))
    with connection() as c:
        _, senders, _ = weave_chat_structure(c, 1, [m1])
    assert senders == 0
    assert len(_identities()) == 1
    assert ("telegram", "platform_id", "111") in _identifiers_of(p)


def test_chat_sender_coocurre_con_lo_extraido() -> None:
    # el remitente del mensaje co-ocurre con el hecho extraído de ESE mensaje, sin mención alguna.
    # (el mensaje también trae su canal → 2 pistas: remitente↔gasto y canal↔gasto; la
    # remitente↔canal la suprime el participa_en confirmed del par.)
    src = _source("telegram", "tg")
    mid = _inbox(src, "m1", _tg_payload(111, display_name="Juan Niebla"))
    fin = _finance("Rappi", [mid])
    with connection() as c:
        _, senders, _ = weave_chat_structure(c, 1, [mid])  # paso 5: remitente + canal + participa
        generate_cooccurrence(c, 1)  # paso 7
        edges = list_edges(c, 1, producer="inbox")
    assert senders == 1
    sender_id = _identities()[0][0]
    assert len(edges) == 2
    pares = [_pair(e) for e in edges]
    assert {("finance", fin), ("identidades:person", sender_id)} in pares
    for e in edges:
        assert e.verdict == "ambiguous"
        assert e.relation_type == "co-ocurrencia"


def test_email_remitente_conocido_coocurre() -> None:
    p = _person("Ana Rivas")
    _identifier(p, "email", "email", "ana@rivas.co")
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("Ana@Rivas.co"))  # case-insensitive por lower()
    fin = _finance("Uber", [mid])
    with connection() as c:
        generate_cooccurrence(c, 1)  # email solo RESUELVE (sin weave_chat_structure)
        edges = list_edges(c, 1, producer="inbox")
    assert len(edges) == 1
    assert _pair(edges[0]) == {("finance", fin), ("identidades:person", p)}


def test_email_remitente_desconocido_no_crea_ni_coocurre() -> None:
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("nadie@desconocido.com"))
    _finance("Uber", [mid])
    with connection() as c:
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert _identities() == []  # email NUNCA crea
    assert edges == []  # un solo vértice en el mensaje → sin pares


def test_social_cuenta_conocida_coocurre() -> None:
    p = _person("La Cuenta")
    _identifier(p, "instagram", "handle", "lacuenta")
    src = _source("apify_instagram", "ig")
    mid = _inbox(
        src,
        "m1",
        {"platform": "instagram", "account": "LaCuenta", "post_id": "p1", "text": "post"},
    )
    fin = _finance("Tienda", [mid])
    with connection() as c:
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert len(edges) == 1
    assert _pair(edges[0]) == {("finance", fin), ("identidades:person", p)}


def test_social_handle_platform_unknown_no_matchea() -> None:
    # un handle manual con platform='unknown' NO resuelve al remitente social (estricto a
    # propósito: el match exige la plataforma real).
    p = _person("La Cuenta")
    _identifier(p, "unknown", "handle", "lacuenta")
    src = _source("apify_instagram", "ig")
    mid = _inbox(
        src,
        "m1",
        {"platform": "instagram", "account": "lacuenta", "post_id": "p1", "text": "post"},
    )
    _finance("Tienda", [mid])
    with connection() as c:
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert edges == []
