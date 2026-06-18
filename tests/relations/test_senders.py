"""Remitente como identidad de PRIMERA CLASE (Fase 2): el remitente de TODO mensaje se resuelve y se
persiste como avistamiento (`mod_identidades_mentions`, `resolution_method='sender'`) en la
extracción (paso 5), y co-ocurre con lo extraído por el brazo NORMAL de menciones (ya NO por un
brazo derivado al vuelo). Política GENERAL por medio: chat→persona; email→un dominio NUNCA es una
persona (rol/relay=org del dominio; individuo=DESCONOCIDO + afiliación, el tipo lo define un
clasificador; free-mail=desconocido/nada según haya nombre); social→cuenta DESCONOCIDA por handle.
Ver `modules/identidades/senders.py`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text

from memex.core.source import SourceKind
from memex.db import connection
from memex.modules.identidades.senders import (
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
    # ya existe la org con el dominio → un relay del mismo dominio resuelve a ESA org, no crea otra.
    o = _org("Nequi")
    _identifier(o, "domain", "domain", "nequi.com")
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("notifications@nequi.com", "Nequi"))
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 1
    assert len(_identities()) == 1  # no nació otra org
    assert _sender_mentions()[0]["rid"] == o


def test_email_subdominio_corporativo_colapsa_al_registrable() -> None:
    # el identifier 'domain' guarda el dominio REGISTRABLE: un relay de un subdominio
    # (notifications@email.acme.com) crea la org «acme.com», y otro relay de OTRO subdominio
    # (noreply@acme.com) resuelve a la MISMA org (no duplica).
    src = _source("imap", "mail")
    m1 = _inbox(src, "m1", _email_payload("notifications@email.acme.com", "Acme"))
    m2 = _inbox(src, "m2", _email_payload("noreply@acme.com", "Acme"))
    with connection() as c:
        weave_email_senders(c, 1, [m1, m2])
    ids = _identities()
    assert len(ids) == 1  # un solo org para ambos subdominios
    org_id = ids[0][0]
    assert ("domain", "domain", "acme.com") in _identifiers_of(org_id)
    mentions = _sender_mentions()
    assert {m["rid"] for m in mentions} == {org_id}  # ambos correos → la misma org
    assert {m["ids"][0] for m in mentions} == {m1, m2}


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


def test_email_freemail_con_nombre_es_desconocido() -> None:
    # free-mail con nombre de individuo → DESCONOCIDO por su correo (el tipo no se adivina sin
    # lista; el clasificador decide si es persona). NO se crea org (gmail.com no es una org).
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("ana.garcia@gmail.com", "Ana García"))
    fin = _finance("Tienda", [mid])
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 1
    ids = _identities()
    assert len(ids) == 1  # solo la entidad; NO se crea org del free-mail
    eid = ids[0][0]
    assert _identity_kinds() == ["desconocido"]
    # value_norm plegado por Gmail (los puntos se ignoran): ana.garcia → anagarcia
    assert ("email", "email", "anagarcia@gmail.com") in _identifiers_of(eid)
    assert _pair(edges[0]) == {("finance", fin), ("identidades:desconocido", eid)}


def test_email_freemail_sin_nombre_no_crea() -> None:
    # free-mail SIN nombre usable (ni rol/relay) → ruido: no se crea ni co-ocurre.
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("randomguy@gmail.com", ""))
    _finance("Tienda", [mid])
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert n == 0
    assert _identities() == []  # free-mail sin nombre NO crea
    assert _sender_mentions() == []
    assert edges == []  # un solo vértice (finance) → sin pares


def test_email_individuo_corporativo_es_desconocido_afiliado() -> None:
    # un individuo de dominio propio, aunque el nombre PAREZCA persona, NO se adivina: queda
    # DESCONOCIDO + afiliación a la org del dominio. El tipo lo define el clasificador (juez LLM).
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("juan.perez@acme.com", "Juan Pérez"))
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 1
    assert sorted(_identity_kinds()) == [
        "desconocido",
        "organizacion",
    ]  # el remitente (pendiente) + la org del dominio
    mentions = _sender_mentions()
    assert len(mentions) == 1 and mentions[0]["rkind"] == "desconocido"
    eid = mentions[0]["rid"]
    assert ("email", "email", "juan.perez@acme.com") in _identifiers_of(eid)
    with connection() as c:
        aff = c.execute(
            text("SELECT person_id, org_id FROM mod_identidades_person_orgs WHERE user_id = 1")
        ).all()
        org_dom = c.execute(
            text(
                "SELECT identity_id FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = 'domain' AND value_norm = 'acme.com'"
            )
        ).scalar()
    assert org_dom is not None
    assert len(aff) == 1
    assert int(aff[0][0]) == eid  # la afiliación va de la entidad (pendiente)...
    assert int(aff[0][1]) == int(org_dom)  # ...a la org del dominio


def test_email_varios_individuos_mismo_dominio_una_org() -> None:
    # varios individuos distintos @acme.com → varios DESCONOCIDO + UNA sola org (keyed por dominio,
    # no fragmenta); un relay (notifications@) es la org. Ningún individuo se colapsa en la org.
    src = _source("imap", "mail")
    m1 = _inbox(src, "m1", _email_payload("juan.perez@acme.com", "Juan Pérez"))
    m2 = _inbox(src, "m2", _email_payload("maria.lopez@acme.com", "María López"))
    m3 = _inbox(src, "m3", _email_payload("notifications@acme.com", "Acme"))  # relay → la org
    with connection() as c:
        weave_email_senders(c, 1, [m1, m2, m3])
    assert sorted(_identity_kinds()) == ["desconocido", "desconocido", "organizacion"]
    with connection() as c:
        domains = c.execute(
            text(
                "SELECT identity_id FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = 'domain' AND value_norm = 'acme.com'"
            )
        ).all()
    assert len(domains) == 1  # una sola org tiene el dominio
    assert sorted(m["rkind"] for m in _sender_mentions()) == [
        "desconocido",
        "desconocido",
        "organizacion",
    ]


def test_email_individuo_corporativo_ambiguo_es_desconocido_afiliado() -> None:
    # un buzón de DEPENDENCIA (local-part ambiguo + from.name de unidad org) en dominio propio NO se
    # adivina persona: queda DESCONOCIDO, afiliado a la org. La arista `afiliado` proyecta bajo su
    # slug (identidades:desconocido), no huérfana (fix _afiliacion_pairs).
    src = _source("imap", "mail")
    mid = _inbox(
        src, "m1", _email_payload("ielec@javeriana.edu.co", "Carrera de Ingeniería Electrónica")
    )
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        edges = list_edges(c, 1)  # solo la arista afiliado (sin co-ocurrencia)
    assert n == 1
    assert sorted(_identity_kinds()) == ["desconocido", "organizacion"]  # el buzón + la org dominio
    mentions = _sender_mentions()
    assert len(mentions) == 1 and mentions[0]["rkind"] == "desconocido"
    eid = mentions[0]["rid"]
    assert ("email", "email", "ielec@javeriana.edu.co") in _identifiers_of(eid)
    with connection() as c:
        org_dom = c.execute(
            text(
                "SELECT identity_id FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = 'domain' AND value_norm = 'javeriana.edu.co'"
            )
        ).scalar()
    assert org_dom is not None
    assert len(edges) == 1
    assert _pair(edges[0]) == {
        ("identidades:desconocido", eid),
        ("identidades:org", int(org_dom)),
    }


def test_email_egerlein_reusa_persona_existente() -> None:
    # egerlein@ ya conocido por su email (persona existente) → resuelve a ESA persona; el email
    # exacto gana antes del gate de tipo → NO crea org ni un desconocido.
    p = _person("Eduardo Gerlein")
    _identifier(p, "email", "email", "egerlein@javeriana.edu.co")
    src = _source("imap", "mail")
    mid = _inbox(
        src, "m1", _email_payload("egerlein@javeriana.edu.co", "Eduardo Andres Gerlein Reyes")
    )
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 1
    assert _identity_kinds() == ["persona"]
    assert _sender_mentions()[0]["rid"] == p


def test_email_varios_buzones_depto_mismo_dominio_una_org() -> None:
    # dos buzones de dependencia distintos del mismo dominio → DOS desconocidos + UNA sola org.
    src = _source("imap", "mail")
    m1 = _inbox(
        src, "m1", _email_payload("ielec@javeriana.edu.co", "Carrera de Ingeniería Electrónica")
    )
    m2 = _inbox(
        src, "m2", _email_payload("viceacad@javeriana.edu.co", "Vicerrectoría Académica PUJ")
    )
    with connection() as c:
        weave_email_senders(c, 1, [m1, m2])
    assert sorted(_identity_kinds()) == ["desconocido", "desconocido", "organizacion"]
    with connection() as c:
        domains = c.execute(
            text(
                "SELECT identity_id FROM mod_identidades_identifiers "
                "WHERE user_id = 1 AND kind = 'domain' AND value_norm = 'javeriana.edu.co'"
            )
        ).all()
    assert len(domains) == 1  # una sola org para ambos deptos


def test_email_freemail_nombre_tipo_org_es_desconocido() -> None:
    # free-mail con nombre tipo-ORG (no representa a una org del dominio) → DESCONOCIDO, sin afil.
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("rh.global@gmail.com", "Departamento de Gestión Humana"))
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 1
    assert _identity_kinds() == ["desconocido"]  # ni persona ni org del free-mail
    assert _sender_mentions()[0]["rkind"] == "desconocido"
    with connection() as c:
        aff = c.execute(
            text("SELECT count(*) FROM mod_identidades_person_orgs WHERE user_id = 1")
        ).scalar()
    assert aff == 0  # free-mail no tiene org → sin afiliación


# --- social ------------------------------------------------------------------------------ #


def test_social_desconocido_crea_cuenta_y_coocurre() -> None:
    # cuenta social nueva → DESCONOCIDO (no se adivina org; el tipo se define luego). Co-ocurre.
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
    acc_id, display = ids[0]
    assert display == "LaCuenta"
    assert _identity_kinds() == ["desconocido"]
    assert ("instagram", "handle", "lacuenta") in _identifiers_of(acc_id)
    assert _pair(edges[0]) == {("finance", fin), ("identidades:desconocido", acc_id)}


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
    # plataforma): se crea una cuenta nueva (DESCONOCIDO) con el handle en la plataforma real.
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
    assert len(ids) == 2  # la persona manual + la cuenta nueva (desconocido)
    new_acc = next(i for i, _ in ids if i != p)
    assert ("instagram", "handle", "lacuenta") in _identifiers_of(new_acc)
    assert _pair(edges[0]) == {("finance", fin), ("identidades:desconocido", new_acc)}


# --- dispatcher -------------------------------------------------------------------------- #


def test_dispatcher_rutea_por_kind() -> None:
    me = _inbox(_source("imap", "mail"), "e1", _email_payload("notifications@acme.com", "Acme"))
    mc = _inbox(_source("telegram", "tg"), "c1", _tg_payload(222, display_name="Pedro"))
    ms = _inbox(_source("apify_instagram", "ig"), "s1", _social_payload("instagram", "marca"))
    with connection() as c:
        weave_sender_structure(c, 1, [me], SourceKind.EMAIL)
        weave_sender_structure(c, 1, [mc], SourceKind.CHAT)
        weave_sender_structure(c, 1, [ms], SourceKind.SOCIAL)
    # email rol/relay→org; chat→persona; social→desconocido (cuenta sin tipo definido).
    assert sorted(_identity_kinds()) == ["desconocido", "organizacion", "persona"]
    assert len(_sender_mentions()) == 3
