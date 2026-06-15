"""Remitente como identidad de PRIMERA CLASE (Fase 2): el remitente de TODO mensaje se resuelve y se
persiste como avistamiento (`mod_identidades_mentions`, `resolution_method='sender'`) en la
extracción (paso 5), y co-ocurre con lo extraído por el brazo NORMAL de menciones (ya NO por un
brazo derivado al vuelo). Política de creación asimétrica por medio: chat→persona, email corp→org
por dominio (free-mail→persona, sin crear), social→org por handle. Ver
`modules/identidades/senders.py`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.core.source import SourceKind
from memex.db import connection
from memex.modules.identidades.senders import (
    backfill_senders,
    renormalize_domain_identifiers,
    weave_chat_structure,
    weave_email_senders,
    weave_sender_structure,
    weave_social_senders,
)
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


def _social_payload(platform: str, account: str) -> dict[str, Any]:
    return {"platform": platform, "account": account, "post_id": "p1", "text": "post"}


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


def _org(name: str) -> int:
    return int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'organizacion', :n) RETURNING id",
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


def _extracted(inbox_id: int, slug: str = "identidades") -> None:
    """Marca un mensaje como YA procesado (cursor en module_extractions) — lo que hace elegible al
    backfill (resolvió su gate de relevancia/blacklist en su momento)."""
    _exec(
        "INSERT INTO module_extractions (user_id, module_slug, inbox_id) VALUES (1, :s, :i)",
        s=slug,
        i=inbox_id,
    )


def _identities() -> list[tuple[int, str]]:
    with connection() as c:
        return [
            (int(r.id), str(r.display_name))
            for r in c.execute(
                text("SELECT id, display_name FROM mod_identidades WHERE user_id = 1 ORDER BY id")
            ).all()
        ]


def _identity_kinds() -> list[str]:
    with connection() as c:
        return [
            str(r.kind)
            for r in c.execute(
                text("SELECT kind FROM mod_identidades WHERE user_id = 1 ORDER BY id")
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


def _sender_mentions() -> list[dict[str, Any]]:
    """Avistamientos de remitente persistidos (resolution_method='sender')."""
    with connection() as c:
        return [
            {
                "rid": int(r["resolved_identity_id"]),
                "rkind": str(r["resolved_kind"]),
                "ids": [int(x) for x in r["source_inbox_ids"]],
            }
            for r in c.execute(
                text(
                    "SELECT resolved_identity_id, resolved_kind, source_inbox_ids "
                    "FROM mod_identidades_mentions "
                    "WHERE user_id = 1 AND resolution_method = 'sender' ORDER BY id"
                )
            )
            .mappings()
            .all()
        ]


def _pair(e: Any) -> set[tuple[str, int]]:
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}


# --- chat -------------------------------------------------------------------------------- #


def test_chat_sender_desconocido_se_crea_con_mencion_idempotente() -> None:
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
    mentions = _sender_mentions()
    assert len(mentions) == 1
    assert mentions[0] == {"rid": iid, "rkind": "persona", "ids": [m1]}
    with connection() as c:
        _, senders2, _ = weave_chat_structure(c, 1, [m1])  # re-correr: ya existe, no duplica
    assert senders2 == 0
    assert len(_identities()) == 1
    assert len(_sender_mentions()) == 1  # la mención NO se re-inserta


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
    assert _sender_mentions() == []
    with connection() as c:
        assert list_edges(c, 1) == []


def test_enriquecimiento_por_username_no_crea() -> None:
    # el username ya era identifier de una identidad → se le ata el platform_id (no nace otra) y la
    # mención del remitente apunta a esa identidad.
    p = _person("Juan Niebla")
    _identifier(p, "telegram", "handle", "juanito")
    src = _source("telegram", "tg")
    m1 = _inbox(src, "m1", _tg_payload(111, username="juanito", display_name="Juan N"))
    with connection() as c:
        _, senders, _ = weave_chat_structure(c, 1, [m1])
    assert senders == 0
    assert len(_identities()) == 1
    assert ("telegram", "platform_id", "111") in _identifiers_of(p)
    mentions = _sender_mentions()
    assert len(mentions) == 1 and mentions[0]["rid"] == p


def test_chat_sender_coocurre_via_mencion() -> None:
    # el remitente co-ocurre con el hecho extraído de su mensaje, ahora vía la mención persistida.
    # 2 pistas: remitente↔gasto y canal↔gasto (remitente↔canal la suprime el participa_en).
    src = _source("telegram", "tg")
    mid = _inbox(src, "m1", _tg_payload(111, display_name="Juan Niebla"))
    fin = _finance("Rappi", [mid])
    with connection() as c:
        _, senders, _ = weave_chat_structure(c, 1, [mid])  # paso 5
        generate_cooccurrence(c, 1)  # paso 7
        edges = list_edges(c, 1, producer="inbox")
    assert senders == 1
    sender_id = _identities()[0][0]
    assert len(edges) == 2
    pares = [_pair(e) for e in edges]
    assert {("finance", fin), ("identidades:person", sender_id)} in pares
    mentions = _sender_mentions()
    assert len(mentions) == 1 and mentions[0]["rid"] == sender_id


# --- email ------------------------------------------------------------------------------- #


def test_email_corporativo_conocido_por_email_resuelve_persona() -> None:
    # un contacto corporativo real (email exacto NO-role conocido) gana sobre el dominio → persona.
    p = _person("Ana Rivas")
    _identifier(p, "email", "email", "ana@rivas.co")
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("Ana@Rivas.co", "Ana Rivas"))  # case-insensitive
    fin = _finance("Uber", [mid])
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 1
    assert len(_identities()) == 1  # NO se creó org del dominio
    assert len(edges) == 1
    assert _pair(edges[0]) == {("finance", fin), ("identidades:person", p)}


def test_email_corporativo_desconocido_crea_org_y_coocurre() -> None:
    # remitente de servicio de un dominio corporativo → crea la ORG del dominio + co-ocurre.
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("notifications@nequi.com", "Nequi"))
    fin = _finance("Compra", [mid])
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 1
    ids = _identities()
    assert len(ids) == 1
    org_id, display = ids[0]
    assert display == "Nequi"
    assert ("domain", "domain", "nequi.com") in _identifiers_of(org_id)
    assert len(edges) == 1
    assert _pair(edges[0]) == {("finance", fin), ("identidades:org", org_id)}


def test_email_corporativo_dominio_conocido_no_duplica() -> None:
    # ya existe la org con el dominio → otro correo del mismo dominio resuelve, no crea otra.
    o = _org("Nequi")
    _identifier(o, "domain", "domain", "nequi.com")
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("soporte@nequi.com", "Nequi Soporte"))
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 1
    assert len(_identities()) == 1  # no nació otra org
    assert _sender_mentions()[0]["rid"] == o


def test_email_subdominio_corporativo_colapsa_al_registrable() -> None:
    # el identifier 'domain' guarda el dominio REGISTRABLE: un correo de un subdominio
    # (notifications@email.acme.com) crea la org «acme.com», y otro de OTRO subdominio
    # (billing@acme.com) resuelve a la MISMA org (no duplica).
    src = _source("imap", "mail")
    m1 = _inbox(src, "m1", _email_payload("notifications@email.acme.com", "Acme"))
    m2 = _inbox(src, "m2", _email_payload("billing@acme.com", "Acme Billing"))
    with connection() as c:
        weave_email_senders(c, 1, [m1, m2])
    ids = _identities()
    assert len(ids) == 1  # un solo org para ambos subdominios
    org_id = ids[0][0]
    assert ("domain", "domain", "acme.com") in _identifiers_of(org_id)
    mentions = _sender_mentions()
    assert {m["rid"] for m in mentions} == {org_id}  # ambos correos → la misma org
    assert {m["ids"][0] for m in mentions} == {m1, m2}


def test_renormalize_domain_identifiers_colapsa_e_idempotente() -> None:
    # un identifier 'domain' viejo con host completo se colapsa al registrable; re-correr no cambia.
    o = _org("OpenAI")
    _identifier(o, "domain", "domain", "tm.openai.com")  # valor viejo (host completo)
    with connection() as c:
        changed = renormalize_domain_identifiers(c, 1)
    assert changed == 1
    assert ("domain", "domain", "openai.com") in _identifiers_of(o)
    assert ("domain", "domain", "tm.openai.com") not in _identifiers_of(o)
    with connection() as c:
        changed2 = renormalize_domain_identifiers(c, 1)  # idempotente
    assert changed2 == 0


def test_renormalize_domain_identifiers_dedup_colision() -> None:
    # misma identidad, dos dominios que colapsan al mismo registrable → queda uno (UNIQUE intacta)
    o = _org("OpenAI")
    _identifier(o, "domain", "domain", "openai.com")
    _identifier(o, "domain", "domain", "tm.openai.com")  # colapsa a openai.com (duplicado)
    with connection() as c:
        changed = renormalize_domain_identifiers(c, 1)
    assert changed == 1  # el duplicado se borró
    domains = {vn for p, k, vn in _identifiers_of(o) if k == "domain"}
    assert domains == {"openai.com"}


def test_email_freemail_conocido_resuelve_persona() -> None:
    p = _person("Ana")
    _identifier(p, "email", "email", "ana@gmail.com")
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("ana@gmail.com", "Ana"))
    fin = _finance("Tienda", [mid])
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 1
    assert len(_identities()) == 1
    assert _pair(edges[0]) == {("finance", fin), ("identidades:person", p)}


def test_email_freemail_desconocido_no_crea_ni_coocurre() -> None:
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("random.person@gmail.com", "Random"))
    _finance("Tienda", [mid])
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert n == 0
    assert _identities() == []  # free-mail desconocido NO crea
    assert _sender_mentions() == []
    assert edges == []  # un solo vértice en el mensaje → sin pares


# --- social ------------------------------------------------------------------------------ #


def test_social_desconocido_crea_org_y_coocurre() -> None:
    src = _source("apify_instagram", "ig")
    mid = _inbox(src, "m1", _social_payload("instagram", "LaCuenta"))
    fin = _finance("Tienda", [mid])
    with connection() as c:
        n = weave_social_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 1
    ids = _identities()
    assert len(ids) == 1
    org_id, display = ids[0]
    assert display == "LaCuenta"
    assert ("instagram", "handle", "lacuenta") in _identifiers_of(org_id)
    assert _pair(edges[0]) == {("finance", fin), ("identidades:org", org_id)}


def test_social_conocido_resuelve() -> None:
    p = _person("La Cuenta")
    _identifier(p, "instagram", "handle", "lacuenta")
    src = _source("apify_instagram", "ig")
    mid = _inbox(src, "m1", _social_payload("instagram", "LaCuenta"))
    fin = _finance("Tienda", [mid])
    with connection() as c:
        n = weave_social_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 1
    assert len(_identities()) == 1
    assert _pair(edges[0]) == {("finance", fin), ("identidades:person", p)}


def test_social_platform_unknown_no_resuelve_crea_nueva() -> None:
    # un handle manual con platform='unknown' NO resuelve el post de instagram (estricto por
    # plataforma): se crea una org nueva con el handle en la plataforma real.
    p = _person("La Cuenta")
    _identifier(p, "unknown", "handle", "lacuenta")
    src = _source("apify_instagram", "ig")
    mid = _inbox(src, "m1", _social_payload("instagram", "lacuenta"))
    fin = _finance("Tienda", [mid])
    with connection() as c:
        n = weave_social_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 1
    ids = _identities()
    assert len(ids) == 2  # la persona manual + la org nueva
    new_org = next(i for i, _ in ids if i != p)
    assert ("instagram", "handle", "lacuenta") in _identifiers_of(new_org)
    assert _pair(edges[0]) == {("finance", fin), ("identidades:org", new_org)}


# --- dispatcher -------------------------------------------------------------------------- #


def test_dispatcher_rutea_por_kind() -> None:
    me = _inbox(_source("imap", "mail"), "e1", _email_payload("notifications@acme.com", "Acme"))
    mc = _inbox(_source("telegram", "tg"), "c1", _tg_payload(222, display_name="Pedro"))
    ms = _inbox(_source("apify_instagram", "ig"), "s1", _social_payload("instagram", "marca"))
    with connection() as c:
        weave_sender_structure(c, 1, [me], SourceKind.EMAIL)
        weave_sender_structure(c, 1, [mc], SourceKind.CHAT)
        weave_sender_structure(c, 1, [ms], SourceKind.SOCIAL)
    assert sorted(_identity_kinds()) == ["organizacion", "organizacion", "persona"]
    assert len(_sender_mentions()) == 3


# --- backfill ---------------------------------------------------------------------------- #


def test_backfill_senders_solo_procesados() -> None:
    # email corporativo + chat + social YA procesados (module_extractions) → se backfillean; un
    # correo SIN procesar (sin cursor) se ignora (no pasó el gate en su momento).
    me = _inbox(_source("imap", "mail"), "e1", _email_payload("notifications@nequi.com", "Nequi"))
    mc = _inbox(_source("telegram", "tg"), "c1", _tg_payload(333, display_name="Carla"))
    ms = _inbox(_source("instagram", "ig"), "s1", _social_payload("instagram", "marca"))
    unprocessed = _inbox(_source("imap", "mail2"), "e2", _email_payload("info@otra.com", "Otra"))
    for mid in (me, mc, ms):
        _extracted(mid)
    with connection() as c:
        out = backfill_senders(c, 1)
    assert out == {"email": 1, "chat": 1, "social": 1}
    assert sorted(_identity_kinds()) == ["organizacion", "organizacion", "persona"]
    mentions = _sender_mentions()
    assert len(mentions) == 3
    assert {m["ids"][0] for m in mentions} == {me, mc, ms}  # el no-procesado quedó sin mención
    assert unprocessed not in {m["ids"][0] for m in mentions}
    with connection() as c:  # idempotente
        out2 = backfill_senders(c, 1)
    assert out2 == {"email": 1, "chat": 1, "social": 1}
    assert len(_sender_mentions()) == 3
