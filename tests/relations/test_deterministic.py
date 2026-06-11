"""Paso de relaciones deterministas (Fase 2): pistas de co-ocurrencia (mismo correo, directo y
transitivo) + afiliación real persona↔org; idempotencia; tope de fan-out; supresión/poda de
pistas redundantes con una confirmada del mismo par.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.relations.deterministic import build_relations
from memex.relations.edges import list_edges, resolve_edge
from memex.relations.vertices import list_vertices


def _exec(sql: str, **params: Any) -> Any:
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def _finance(merchant: str, inbox_ids: list[int], identity_id: int | None = None) -> int:
    """Crea una transacción cruda + su consolidado + el link. El VÉRTICE es el consolidado; su
    procedencia de inbox es transitiva (link → crudo.source_inbox_ids). `identity_id` setea el
    `counterparty_identity_id` del consolidado (para la arista de contraparte)."""
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
            "occurred_at, counterparty, counterparty_identity_id) "
            "VALUES (1, 'egreso', 100, 'COP', NOW(), :m, :iid) RETURNING id",
            m=merchant,
            iid=identity_id,
        )
    )
    _exec(
        "INSERT INTO mod_finance_transaction_links (user_id, consolidated_id, transaction_id) "
        "VALUES (1, :c, :t)",
        c=cons,
        t=crudo,
    )
    return cons


def _hack(name: str, inbox_ids: list[int]) -> int:
    return int(
        _exec(
            "INSERT INTO mod_hackathones_events (user_id, source_inbox_ids, name) "
            "VALUES (1, :ids, :n) RETURNING id",
            ids=inbox_ids,
            n=name,
        )
    )


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


def _producto(name: str) -> int:
    return int(
        _exec(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'producto', :n) RETURNING id",
            n=name,
        )
    )


def _link_person_org(person_id: int, org_id: int) -> None:
    _exec(
        "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id) VALUES (1, :p, :o)",
        p=person_id,
        o=org_id,
    )


def _mention(identity_id: int, inbox_ids: list[int], kind: str = "persona") -> None:
    # `kind` es cosmético: el slug del vértice sale de `mod_identidades.kind` (no de la mención);
    # lo que importa es a qué identidad apunta `resolved_identity_id`.
    _exec(
        "INSERT INTO mod_identidades_mentions "
        "(user_id, source_inbox_ids, mentioned_name, resolved_kind, resolved_identity_id) "
        "VALUES (1, :ids, 'X', :k, :p)",
        ids=inbox_ids,
        k=kind,
        p=identity_id,
    )


def _calendar(title: str, inbox_ids: list[int]) -> int:
    """Crea un evento crudo + su consolidado + el link. El VÉRTICE es el consolidado (devuelto)."""
    crudo = int(
        _exec(
            "INSERT INTO mod_calendar_events (user_id, source_inbox_ids, title, starts_on) "
            "VALUES (1, :ids, :t, DATE '2026-07-01') RETURNING id",
            ids=inbox_ids,
            t=title,
        )
    )
    cons = int(
        _exec(
            "INSERT INTO mod_calendar_consolidated (user_id, title, starts_on) "
            "VALUES (1, :t, DATE '2026-07-01') RETURNING id",
            t=title,
        )
    )
    _exec(
        "INSERT INTO mod_calendar_event_links (user_id, consolidated_id, event_id) "
        "VALUES (1, :c, :e)",
        c=cons,
        e=crudo,
    )
    return cons


def _pair(e: Any) -> set[tuple[str, int]]:
    return {(e.src.slug, e.src.id), (e.dst.slug, e.dst.id)}


def test_cooccurrence_pista_mismo_correo() -> None:
    fin = _finance("Rappi", [5])
    hack = _hack("HackBogota", [5])
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats.cooccurrence_pistas == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "inbox"
    assert e.status == "pista"
    assert e.relation_type == "co-ocurrencia"
    assert _pair(e) == {("finance", fin), ("hackathones", hack)}


def test_sin_correo_comun_no_hay_pista() -> None:
    _finance("Rappi", [5])
    _hack("Hack", [6])
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats.cooccurrence_pistas == 0
    assert edges == []


def test_calendar_transitivo() -> None:
    # el vértice calendar es el consolidado; comparte correo con el gasto vía el crudo
    fin = _finance("Rappi", [7])
    cal = _calendar("Reunión", [7])
    with connection() as c:
        build_relations(c, 1)
        edges = list_edges(c, 1)
    assert len(edges) == 1
    assert _pair(edges[0]) == {("finance", fin), ("calendar", cal)}


def test_identidades_transitivo_via_mencion() -> None:
    fin = _finance("Rappi", [8])
    p = _person("Juan")
    _mention(p, [8])
    with connection() as c:
        build_relations(c, 1)
        edges = list_edges(c, 1)
    assert len(edges) == 1
    assert _pair(edges[0]) == {("finance", fin), ("identidades:person", p)}


def test_cooccurrence_persona_org_mismo_correo() -> None:
    # dos identidades (persona + org) co-mencionadas en el MISMO correo → una pista entre ellas.
    p = _person("Juan")
    o = _org("Acme")
    _mention(p, [9])
    _mention(o, [9], kind="organizacion")
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats.cooccurrence_pistas == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "inbox"
    assert e.status == "pista"
    assert e.relation_type == "co-ocurrencia"
    assert _pair(e) == {("identidades:person", p), ("identidades:org", o)}


def test_cooccurrence_misma_identidad_dos_menciones_no_edge() -> None:
    # dos menciones del MISMO correo que resuelven a la MISMA identidad → sin auto-enlace
    # (el set de `Ref` colapsa el vértice repetido; queda 1 < 2 vértices).
    p = _person("Ana")
    _mention(p, [10])
    _mention(p, [10])
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats.cooccurrence_pistas == 0
    assert edges == []


def test_cooccurrence_identidades_respeta_cap() -> None:
    # 3 identidades del mismo correo con cap=2 → ese mensaje se salta (la co-ocurrencia es ruido);
    # arriba del umbral toma el relevo el handler LLM (relations_llm), no este paso determinista.
    a = _person("A")
    b = _person("B")
    c_ = _person("C")
    _mention(a, [11])
    _mention(b, [11])
    _mention(c_, [11])
    with connection() as c:
        stats = build_relations(c, 1, cooccurrence_cap=2)
        edges = list_edges(c, 1)
    assert stats.high_fanout_skipped == 1
    assert stats.cooccurrence_pistas == 0
    assert edges == []


def test_afiliacion_real_persona_org() -> None:
    p = _person("Juan")
    o = _org("Acme")
    _link_person_org(p, o)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.afiliacion_reales == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.status == "confirmed"
    assert e.relation_type == "afiliado"
    assert (e.src.slug, e.src.id) == ("identidades:person", p)
    assert (e.dst.slug, e.dst.id) == ("identidades:org", o)


def _set_parent(child: int, parent: int) -> None:
    _exec("UPDATE mod_identidades SET parent_identity_id = :p WHERE id = :c", p=parent, c=child)


def test_pertenencia_real_sub_padre() -> None:
    parent = _org("Valve Corporation")
    child = _org("Steam")
    _set_parent(child, parent)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.pertenencia_reales == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.status == "confirmed"
    assert e.relation_type == "pertenece_a"
    assert (e.src.slug, e.src.id) == ("identidades:org", child)  # dirigida: hijo → padre
    assert (e.dst.slug, e.dst.id) == ("identidades:org", parent)


def test_pertenencia_producto_a_empresa() -> None:
    # producto→empresa con kinds reales: la arista usa el slug identidades:producto del hijo
    parent = _org("Valve Corporation")
    child = _producto("Steam")
    _set_parent(child, parent)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.pertenencia_reales == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.relation_type == "pertenece_a"
    assert (e.src.slug, e.src.id) == ("identidades:producto", child)
    assert (e.dst.slug, e.dst.id) == ("identidades:org", parent)


def test_cooccurrence_producto_sobrevive_poda() -> None:
    # pista con vértice producto: el slug nuevo PROYECTA (NODE_SOURCES) → la poda de huérfanas no
    # la barre en el siguiente build (la trampa que motivó adelantar la plomería de slugs).
    prod = _producto("Hearthstone")
    p = _person("Rodion")
    _mention(prod, [21], kind="producto")
    _mention(p, [21])
    with connection() as c:
        stats = build_relations(c, 1)
    assert stats.cooccurrence_pistas == 1
    with connection() as c:
        build_relations(c, 1)  # segundo build: la arista no es huérfana, sobrevive
        edges = list_edges(c, 1)
    assert len(edges) == 1
    assert _pair(edges[0]) == {("identidades:producto", prod), ("identidades:person", p)}


def test_idempotente() -> None:
    _finance("Rappi", [5])
    _hack("Hack", [5])
    with connection() as c:
        build_relations(c, 1)
        n1 = len(list_edges(c, 1))
    with connection() as c:
        build_relations(c, 1)
        n2 = len(list_edges(c, 1))
    assert n1 == n2 == 1


def test_high_fanout_se_salta() -> None:
    # 3 vértices del mismo correo con cap=2 → ese mensaje se salta (co-ocurrencia = ruido)
    _finance("A", [5])
    _finance("B", [5])
    _finance("C", [5])
    with connection() as c:
        stats = build_relations(c, 1, cooccurrence_cap=2)
        edges = list_edges(c, 1)
    assert stats.high_fanout_skipped == 1
    assert stats.cooccurrence_pistas == 0
    assert edges == []


def test_contraparte_real_cobro_a_identidad() -> None:
    # un cobro CONSOLIDADO cuya contraparte resolvió a una identidad → arista confirmed
    # cobro→identidad (el enlace por identidad entre finanzas y el directorio).
    org = _org("Uber")
    fin = _finance("Uber", [12], identity_id=org)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="finance")
    assert stats.contraparte_reales == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "finance"
    assert e.status == "confirmed"
    assert e.relation_type == "contraparte"
    assert (e.src.slug, e.src.id) == ("finance", fin)  # dirigida: cobro → quién cobró/pagó
    assert (e.dst.slug, e.dst.id) == ("identidades:org", org)


# ----- reconciliación: una corrección del directorio borra la arista vieja -------- #


def test_reconcile_pertenencia_quitada() -> None:
    # quitar el padre (set-parent --clear / PATCH) debe borrar la arista en el siguiente build:
    # ambos vértices siguen vivos → prune_orphan_edges no la ve; la reconciliación sí.
    parent = _org("Valve Corporation")
    child = _producto("Celeste")
    _set_parent(child, parent)
    with connection() as c:
        build_relations(c, 1)
        assert len(list_edges(c, 1, producer="identidades")) == 1
    _exec("UPDATE mod_identidades SET parent_identity_id = NULL WHERE id = :c", c=child)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.stale_pruned == 1
    assert edges == []


def test_reconcile_pertenencia_cambiada() -> None:
    # cambiar de padre: la arista al padre viejo se borra, queda solo la del nuevo.
    viejo = _org("Uber")
    nuevo = _org("Maddy Makes Games")
    child = _producto("Celeste")
    _set_parent(child, viejo)
    with connection() as c:
        build_relations(c, 1)
    _set_parent(child, nuevo)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = [
            e for e in list_edges(c, 1, producer="identidades") if e.relation_type == "pertenece_a"
        ]
    assert stats.stale_pruned == 1
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:org", nuevo)


def test_reconcile_afiliacion_borrada() -> None:
    p = _person("Juan")
    o = _org("Acme")
    _link_person_org(p, o)
    with connection() as c:
        build_relations(c, 1)
        assert len(list_edges(c, 1, producer="identidades")) == 1
    _exec("DELETE FROM mod_identidades_person_orgs WHERE user_id = 1 AND person_id = :p", p=p)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="identidades")
    assert stats.stale_pruned == 1
    assert edges == []


def test_reconcile_contraparte_reapuntada() -> None:
    # re-resolver la contraparte de un pago: la arista a la identidad vieja se borra.
    vieja = _org("Uber")
    nueva = _org("Uber Colombia")
    fin = _finance("Uber", [12], identity_id=vieja)
    with connection() as c:
        build_relations(c, 1)
        assert len(list_edges(c, 1, producer="finance")) == 1
    _exec(
        "UPDATE mod_finance_consolidated SET counterparty_identity_id = :n WHERE id = :f",
        n=nueva,
        f=fin,
    )
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="finance")
    assert stats.stale_pruned == 1
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:org", nueva)


def test_contraparte_sin_identidad_no_edge() -> None:
    # cobro sin counterparty_identity_id (no resolvió) → no hay arista de contraparte.
    _finance("Comercio X", [13])
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1, producer="finance")
    assert stats.contraparte_reales == 0
    assert edges == []


def test_contraparte_persona() -> None:
    # contraparte persona (ej. una transferencia a alguien) → arista a identidades:person.
    p = _person("Juan Perez")
    fin = _finance("Juan Perez", [14], identity_id=p)
    with connection() as c:
        build_relations(c, 1)
        edges = list_edges(c, 1, producer="finance")
    assert len(edges) == 1
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:person", p)
    assert (edges[0].src.slug, edges[0].src.id) == ("finance", fin)


def test_contraparte_a_producto_via_id() -> None:
    # un counterparty_identity_id que apunta a un producto (vínculo histórico pre-veto, o futuro
    # backfill org→producto) → la arista usa el slug identidades:producto y no queda huérfana.
    prod = _producto("Steam")
    fin = _finance("Steam", [22], identity_id=prod)
    with connection() as c:
        build_relations(c, 1)
        edges = list_edges(c, 1, producer="finance")
    assert len(edges) == 1
    assert (edges[0].src.slug, edges[0].src.id) == ("finance", fin)
    assert (edges[0].dst.slug, edges[0].dst.id) == ("identidades:producto", prod)


def test_cooccurrence_suprimida_por_confirmada_del_par() -> None:
    # la pista tx↔org duplicaría la contraparte confirmada del MISMO par → se suprime (la
    # conectividad ya la da la real, que pesa 1.0); los demás pares del mensaje SÍ emiten.
    org = _org("Uber")
    fin = _finance("Uber", [15], identity_id=org)
    _mention(org, [15], kind="organizacion")
    hack = _hack("HackPago", [15])
    with connection() as c:
        stats = build_relations(c, 1)
        pistas = list_edges(c, 1, producer="inbox")
    assert stats.contraparte_reales == 1
    assert stats.cooccurrence_pistas == 2
    assert stats.redundant_pruned == 0
    pares = [_pair(e) for e in pistas]
    assert {("finance", fin), ("identidades:org", org)} not in pares
    assert {("finance", fin), ("hackathones", hack)} in pares
    assert {("identidades:org", org), ("hackathones", hack)} in pares


def test_poda_pista_redundante_preexistente() -> None:
    # primer build: tx sin identidad → pista tx↔org normal; luego la contraparte se resuelve →
    # el segundo build confirma la real Y poda la pista redundante del par; el tercero es no-op.
    org = _org("Uber")
    fin = _finance("Uber", [16])
    _mention(org, [16], kind="organizacion")
    with connection() as c:
        stats1 = build_relations(c, 1)
    assert stats1.cooccurrence_pistas == 1
    _exec(
        "UPDATE mod_finance_consolidated SET counterparty_identity_id = :o WHERE id = :f",
        o=org,
        f=fin,
    )
    with connection() as c:
        stats2 = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats2.contraparte_reales == 1
    assert stats2.redundant_pruned == 1
    assert len(edges) == 1  # queda SOLO la contraparte confirmada
    assert edges[0].relation_type == "contraparte"
    assert edges[0].status == "confirmed"
    with connection() as c:
        stats3 = build_relations(c, 1)
        n = len(list_edges(c, 1))
    assert stats3.redundant_pruned == 0
    assert stats3.cooccurrence_pistas == 0
    assert n == 1


def test_orientacion_inversa_tambien_suprime() -> None:
    # la confirmada es afiliado person→org; el par canónico de la pista sería org→person
    # (orden (slug, id): "identidades:org" < "identidades:person") → el par se compara sin
    # orientación y la pista igual se suprime.
    p = _person("Ana")
    o = _org("Acme")
    _link_person_org(p, o)
    _mention(p, [17])
    _mention(o, [17], kind="organizacion")
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats.cooccurrence_pistas == 0
    assert stats.redundant_pruned == 0
    assert len(edges) == 1
    assert edges[0].relation_type == "afiliado"


def test_cooc_promovida_a_confirmed_no_se_poda() -> None:
    # una pista promovida a confirmed (cascada del partidor) NO se poda (el filtro status=pista
    # la excluye) y su par tampoco se re-emite (ya está vouchado).
    _finance("Rappi", [18])
    _hack("HackX", [18])
    with connection() as c:
        build_relations(c, 1)
        eid = list_edges(c, 1)[0].id
        resolve_edge(c, eid, status="confirmed")
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats.redundant_pruned == 0
    assert stats.cooccurrence_pistas == 0
    assert len(edges) == 1
    assert edges[0].status == "confirmed"
    assert edges[0].relation_type == "co-ocurrencia"


def test_build_poda_huerfana_por_tombstone() -> None:
    # fin y hack co-ocurren en el correo 5 → 1 pista; tombstoneamos el consolidado: su vértice
    # desaparece (where NOT deleted) y la arista que lo tocaba queda huérfana → el GC la barre.
    fin = _finance("Rappi", [5])
    _hack("HackBogota", [5])
    with connection() as c:
        build_relations(c, 1)
        assert len(list_edges(c, 1)) == 1
    _exec("UPDATE mod_finance_consolidated SET deleted = TRUE WHERE id = :i", i=fin)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
        live = {(v.slug, v.id) for v in list_vertices(c, 1)}
    assert stats.orphans_pruned == 1
    assert edges == []  # la única arista tocaba el vértice tombstoneado
    # invariante "cero huérfanas": toda arista que quede resuelve a un vértice vivo
    for e in edges:
        assert (e.src.slug, e.src.id) in live
        assert (e.dst.slug, e.dst.id) in live


def test_build_poda_huerfana_por_fila_borrada() -> None:
    # camino hard-delete (en vez de tombstone): el hackatón se borra tras construir → su arista de
    # co-ocurrencia queda huérfana y el GC la barre.
    _finance("Rappi", [6])
    hack = _hack("HackMed", [6])
    with connection() as c:
        build_relations(c, 1)
        assert len(list_edges(c, 1)) == 1
    _exec("DELETE FROM mod_hackathones_events WHERE id = :i", i=hack)
    with connection() as c:
        stats = build_relations(c, 1)
        edges = list_edges(c, 1)
    assert stats.orphans_pruned == 1
    assert edges == []
