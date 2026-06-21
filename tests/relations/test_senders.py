"""Remitente como identidad de PRIMERA CLASE (Fase 2): el remitente de TODO mensaje se resuelve y se
persiste como avistamiento (`mod_identidades_mentions`, `resolution_method='sender'`) en la
extracción (paso 5), y co-ocurre con lo extraído por el brazo NORMAL de menciones (ya NO por un
brazo derivado al vuelo). Política GENERAL por medio: chat→persona; email→un dominio es ATRIBUTO,
no identidad (NO se crea ficha-dominio): rol/relay CON nombre de org = org por NOMBRE + dominio
atado; lo demás corporativo (individuo/buzón no-rol) = leftover, lo captura extracción/resolver;
free-mail = leftover/nada según haya nombre; social→cuenta DESCONOCIDA por handle.
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


def test_email_freemail_con_nombre_es_leftover() -> None:
    # free-mail con nombre → NO se crea stub (un email no es identidad). Queda como leftover: lo
    # crea/decide el resolver. El dedup no inventa la persona ni una org del free-mail.
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("ana.garcia@gmail.com", "Ana García"))
    _finance("Tienda", [mid])
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert n == 0
    assert _identities() == []  # free-mail no crea stub
    assert _sender_mentions() == []
    assert edges == []


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


def test_email_individuo_corporativo_es_leftover() -> None:
    # individuo de dominio propio NO-rol → leftover: el procedimental no adivina persona-vs-org ni
    # crea ficha-dominio (un dominio es ATRIBUTO). Lo captura extracción/resolver con contexto.
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("juan.perez@acme.com", "Juan Pérez"))
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 0
    assert _identities() == []  # ni org-dominio ni persona
    assert _sender_mentions() == []


def test_email_sender_no_depende_de_resolver_enabled() -> None:
    # Se quitó el `defer`: un relay CON nombre de org se resuelve igual con resolver ON/OFF (org por
    # nombre + dominio atado). Guarda contra re-acoplar el sender al resolver.
    from memex.modules.identidades.settings import upsert_settings

    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("notifications@acme.com", "Acme"))  # rol + nombre de org
    with connection() as c:
        upsert_settings(c, 1, resolver_enabled=True)
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 1
    assert _identity_kinds() == ["organizacion"]
    org_id = _sender_mentions()[0]["rid"]
    assert ("domain", "domain", "acme.com") in _identifiers_of(org_id)


def test_email_varios_individuos_corporativos_solo_el_relay_crea_org() -> None:
    # varios @acme.com: los individuos NO-rol quedan leftover (no stub, no contacto); SOLO el relay
    # CON nombre crea la org (por nombre + dominio atado).
    src = _source("imap", "mail")
    m1 = _inbox(src, "m1", _email_payload("juan.perez@acme.com", "Juan Pérez"))
    m2 = _inbox(src, "m2", _email_payload("maria.lopez@acme.com", "María López"))
    m3 = _inbox(src, "m3", _email_payload("notifications@acme.com", "Acme"))  # relay → la org
    with connection() as c:
        weave_email_senders(c, 1, [m1, m2, m3])
    assert _identity_kinds() == ["organizacion"]  # solo la org del relay
    org_id = _sender_mentions()[0]["rid"]
    idf = _identifiers_of(org_id)
    assert ("domain", "domain", "acme.com") in idf
    assert ("email", "email", "juan.perez@acme.com") not in idf  # individuos no-rol → leftover
    assert ("email", "email", "maria.lopez@acme.com") not in idf


def test_email_buzon_dependencia_no_rol_es_leftover() -> None:
    # buzón de dependencia con local-part que NO es rol genérico (ielec@) → leftover. El nombre de
    # la dependencia ("Carrera de Ingeniería Electrónica") lo captura el extractor (el render ahora
    # muestra el email, así que el dominio queda visible).
    src = _source("imap", "mail")
    mid = _inbox(
        src, "m1", _email_payload("ielec@javeriana.edu.co", "Carrera de Ingeniería Electrónica")
    )
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 0
    assert _identities() == []
    assert _sender_mentions() == []


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


def test_email_varios_buzones_depto_no_rol_son_leftover() -> None:
    # dos buzones de dependencia no-rol del mismo dominio → ambos leftover (no se crea ficha-dominio
    # ni org por buzón). Las dependencias las captura el extractor por su nombre.
    src = _source("imap", "mail")
    m1 = _inbox(
        src, "m1", _email_payload("ielec@javeriana.edu.co", "Carrera de Ingeniería Electrónica")
    )
    m2 = _inbox(
        src, "m2", _email_payload("viceacad@javeriana.edu.co", "Vicerrectoría Académica PUJ")
    )
    with connection() as c:
        n = weave_email_senders(c, 1, [m1, m2])
    assert n == 0
    assert _identities() == []


def test_email_freemail_nombre_tipo_org_es_leftover() -> None:
    # free-mail (gmail) aunque el nombre parezca org → NO se crea stub; leftover para el resolver.
    src = _source("imap", "mail")
    mid = _inbox(src, "m1", _email_payload("rh.global@gmail.com", "Departamento de Gestión Humana"))
    with connection() as c:
        n = weave_email_senders(c, 1, [mid])
    assert n == 0
    assert _identities() == []  # free-mail no crea stub
    assert _sender_mentions() == []


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
