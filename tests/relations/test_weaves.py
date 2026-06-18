"""Tejido de las aristas REALES (paso 5): `weave_afiliacion`, `weave_pertenencia`,
`weave_finance_consolidated` (contraparte). Cada módulo las teje al escribir; acá se seedea el dato
y se llama la weave pública directo (idempotente, dirigida, con el slug correcto del extremo)."""

from __future__ import annotations

from memex.db import connection
from memex.relations.deterministic import (
    weave_afiliacion,
    weave_calendar_consolidated,
    weave_finance_consolidated,
    weave_pertenencia,
)
from memex.relations.edges import list_edges
from tests.relations._graph_seed import (
    calendar_declined_setting,
    calendar_event,
    calendar_participant,
    desconocido,
    email_identifier,
    finance,
    link_person_org,
    org,
    person,
    producto,
    set_edge_verdict,
    set_parent,
)


def test_afiliacion_real_persona_org() -> None:
    p = person("Juan")
    o = org("Acme")
    link_person_org(p, o)
    with connection() as c:
        n = weave_afiliacion(c, 1, p)
        edges = list_edges(c, 1, producer="identidades")
    assert n == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.verdict == "confirmed"
    assert e.relation_type == "afiliado"
    assert (e.src.slug, e.src.id) == ("identidades:person", p)
    assert (e.dst.slug, e.dst.id) == ("identidades:org", o)


def test_pertenencia_real_sub_padre() -> None:
    parent = org("Valve Corporation")
    child = org("Steam")
    set_parent(child, parent)
    with connection() as c:
        n = weave_pertenencia(c, 1, child)
        edges = list_edges(c, 1, producer="identidades")
    assert n == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.verdict == "confirmed"
    assert e.relation_type == "pertenece_a"
    assert (e.src.slug, e.src.id) == ("identidades:org", child)  # dirigida: hijo → padre
    assert (e.dst.slug, e.dst.id) == ("identidades:org", parent)


def test_pertenencia_producto_a_empresa() -> None:
    # producto→empresa con kinds reales: la arista usa el slug identidades:producto del hijo.
    parent = org("Valve Corporation")
    child = producto("Steam")
    set_parent(child, parent)
    with connection() as c:
        weave_pertenencia(c, 1, child)
        edges = list_edges(c, 1, producer="identidades")
    assert len(edges) == 1
    e = edges[0]
    assert e.relation_type == "pertenece_a"
    assert (e.src.slug, e.src.id) == ("identidades:producto", child)
    assert (e.dst.slug, e.dst.id) == ("identidades:org", parent)


def test_pertenencia_sin_padre_no_edge() -> None:
    # un hijo sin `parent_identity_id` no teje nada (la weave es no-op).
    child = producto("Celeste")
    with connection() as c:
        n = weave_pertenencia(c, 1, child)
        edges = list_edges(c, 1, producer="identidades")
    assert n == 0
    assert edges == []


def test_contraparte_real_cobro_a_identidad() -> None:
    # un cobro CONSOLIDADO cuya contraparte resolvió a una identidad → arista confirmed
    # cobro→identidad (el enlace por identidad entre finanzas y el directorio).
    o = org("Uber")
    fin = finance("Uber", [12], identity_id=o)
    with connection() as c:
        contraparte, same_event = weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert contraparte == 1
    assert same_event == 0
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "finance"
    assert e.verdict == "confirmed"
    assert e.relation_type == "contraparte"
    assert (e.src.slug, e.src.id) == ("finance", fin)  # dirigida: cobro → quién cobró/pagó
    assert (e.dst.slug, e.dst.id) == ("identidades:org", o)


def test_contraparte_sin_identidad_no_edge() -> None:
    # cobro sin counterparty_identity_id (no resolvió) → no hay arista de contraparte.
    fin = finance("Comercio X", [13])
    with connection() as c:
        contraparte, _ = weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert contraparte == 0
    assert edges == []


def test_contraparte_persona() -> None:
    # contraparte persona (ej. una transferencia a alguien) → arista a identidades:person.
    p = person("Juan Perez")
    fin = finance("Juan Perez", [14], identity_id=p)
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:person", p)
    assert (edges[0].src.slug, edges[0].src.id) == ("finance", fin)


def test_contraparte_a_producto_via_id() -> None:
    # un counterparty_identity_id que apunta a un producto → la arista usa el slug
    # identidades:producto y no queda huérfana.
    prod = producto("Steam")
    fin = finance("Steam", [22], identity_id=prod)
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])
        edges = list_edges(c, 1, producer="finance")
    assert len(edges) == 1
    assert (edges[0].src.slug, edges[0].src.id) == ("finance", fin)
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:producto", prod)


def test_weave_idempotente() -> None:
    # re-tejer no duplica (ON CONFLICT lógico de propose_edge).
    p = person("Ana")
    o = org("Acme")
    link_person_org(p, o)
    with connection() as c:
        weave_afiliacion(c, 1, p)
        weave_afiliacion(c, 1, p)
        edges = list_edges(c, 1, producer="identidades")
    assert len(edges) == 1


# --- calendar: organiza/asiste (evento→identidad) ------------------------------------- #


def test_calendar_organiza_y_asiste() -> None:
    # organizador → «organiza», asistente resuelto por email → «asiste», ambas evento→identidad.
    ana = person("Ana")
    beto = person("Beto")
    email_identifier(ana, "ana@example.com")
    email_identifier(beto, "beto@example.com")
    cons, ev = calendar_event("Reunión")
    calendar_participant(ev, "organizer", "ana@example.com")
    calendar_participant(ev, "attendee", "beto@example.com", response_status="accepted")
    with connection() as c:
        n = weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert n == 2
    by_rel = {e.relation_type: e for e in edges}
    org_e = by_rel["organiza"]
    assert org_e.producer == "calendar"
    assert org_e.verdict == "confirmed" and org_e.provenance == "extracted"
    assert (org_e.src.slug, org_e.src.id) == ("calendar", cons)  # dirección evento→identidad
    assert (org_e.dst.slug, org_e.dst.id) == ("identidades:person", ana)
    asi_e = by_rel["asiste"]
    assert (asi_e.src.slug, asi_e.src.id) == ("calendar", cons)
    assert (asi_e.dst.slug, asi_e.dst.id) == ("identidades:person", beto)


def test_calendar_filtra_self_y_resource() -> None:
    # el dueño (self) está en todos sus eventos = ruido; una sala (resource) no es persona.
    org_id = org("Acme")
    me = person("Yo")
    room = person("Sala A")
    email_identifier(org_id, "org@acme.com")
    email_identifier(me, "me@acme.com")
    email_identifier(room, "room@acme.com")
    _cons, ev = calendar_event("Evento")
    calendar_participant(ev, "organizer", "org@acme.com")
    calendar_participant(ev, "attendee", "me@acme.com", is_self=True)
    calendar_participant(ev, "attendee", "room@acme.com", is_resource=True)
    with connection() as c:
        weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert len(edges) == 1  # solo el organizador; self y resource fuera
    assert edges[0].relation_type == "organiza"
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:org", org_id)


def test_calendar_declined_excluido_por_default_y_con_perilla() -> None:
    ana = person("Ana")  # organizadora que rechazó → igual «organiza»
    beto = person("Beto")  # asistente que rechazó → sin «asiste» por default
    email_identifier(ana, "ana@example.com")
    email_identifier(beto, "beto@example.com")
    _cons, ev = calendar_event("Reunión")
    calendar_participant(ev, "organizer", "ana@example.com", response_status="declined")
    calendar_participant(ev, "attendee", "beto@example.com", response_status="declined")
    with connection() as c:
        weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert {e.relation_type for e in edges} == {"organiza"}  # declined no asiste

    calendar_declined_setting(True)  # con la perilla, el declined entra
    with connection() as c:
        weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert {e.relation_type for e in edges} == {"organiza", "asiste"}
    asi = next(e for e in edges if e.relation_type == "asiste")
    assert (asi.dst.slug, asi.dst.id) == ("identidades:person", beto)


def test_calendar_veta_producto_enlaza_desconocido() -> None:
    # «quién organiza/asiste» es persona/org/desconocido (un email real sin tipo definido vale);
    # un producto no participa de una reunión → vetado.
    prod = producto("Steam")
    desc = desconocido("alguien")
    email_identifier(prod, "steam@valvesoftware.com")
    email_identifier(desc, "alguien@raro.com")
    _cons, ev = calendar_event("Evento")
    calendar_participant(ev, "attendee", "steam@valvesoftware.com", response_status="accepted")
    calendar_participant(ev, "attendee", "alguien@raro.com", response_status="accepted")
    with connection() as c:
        weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert len(edges) == 1  # producto vetado; desconocido enlaza
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:desconocido", desc)


def test_calendar_email_canonicalizado_matchea() -> None:
    # identifier "jdoe@gmail.com" vs participante "J.Doe+promo@gmail.com": ambos colapsan vía
    # norm_identifier (Gmail ignora puntos + quita +tag) → el join exacto matchea.
    ana = person("Ana")
    email_identifier(ana, "jdoe@gmail.com")
    _cons, ev = calendar_event("Evento")
    calendar_participant(ev, "organizer", "J.Doe+promo@gmail.com")
    with connection() as c:
        weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:person", ana)


def test_calendar_idempotente_y_no_resucita_rechazada() -> None:
    ana = person("Ana")
    email_identifier(ana, "ana@example.com")
    _cons, ev = calendar_event("Evento")
    calendar_participant(ev, "organizer", "ana@example.com")
    with connection() as c:
        n1 = weave_calendar_consolidated(c, 1)
        n2 = weave_calendar_consolidated(c, 1)  # misma tx: 2ª corrida no agrega (diff vacío)
        edges = list_edges(c, 1, producer="calendar")
    assert (n1, n2) == (1, 0)
    assert len(edges) == 1

    # un humano rechaza la arista → re-tejer NO la resucita (el diff la ve en `existing`).
    set_edge_verdict("calendar", "rejected")
    with connection() as c:
        n3 = weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert n3 == 0
    assert len(edges) == 1 and edges[0].verdict == "rejected"


def test_calendar_participante_sin_identidad_no_edge() -> None:
    # un email que no está en el directorio → no hay arista (no se crea identidad).
    _cons, ev = calendar_event("Evento")
    calendar_participant(ev, "organizer", "desconocido@nadie.com")
    with connection() as c:
        n = weave_calendar_consolidated(c, 1)
        edges = list_edges(c, 1, producer="calendar")
    assert n == 0
    assert edges == []
