"""Generación de PISTAS de co-ocurrencia (paso 7, `generate_cooccurrence`): pistas del mismo mensaje
(directo y transitivo) + idempotencia + tope de fan-out + supresión de pares ya vouchados +
confirmación (con historial) de pistas redundantes + procedencia acumulada por pista.

`generate_cooccurrence` devuelve `(pistas, saltados, redundantes)`. Las aristas REALES ya NO se
arman acá (las tejen los módulos al escribir): los tests que necesitan una real la tejen con su
`weave_*` ANTES de generar, igual que el pipeline (paso 5 antes del 7).
"""

from __future__ import annotations

from memex.db import connection
from memex.relations.cooccurrence import generate_cooccurrence
from memex.relations.decisions import edge_sources, latest_decisions
from memex.relations.deterministic import weave_afiliacion, weave_finance_consolidated
from memex.relations.edges import list_edges, resolve_edge
from memex.relations.maintenance import reconcile_graph
from tests.relations._graph_seed import (
    calendar,
    finance,
    hack,
    link_person_org,
    mention,
    org,
    pair,
    person,
    producto,
    run,
)


def test_cooccurrence_pista_mismo_correo() -> None:
    fin = finance("Rappi", [5])
    h = hack("HackBogota", [5])
    with connection() as c:
        pistas, _, _ = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert pistas == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.producer == "inbox"
    assert e.verdict == "ambiguous"
    assert e.relation_type == "co-ocurrencia"
    assert pair(e) == {("finance", fin), ("hackathones", h)}


def test_sin_correo_comun_no_hay_pista() -> None:
    finance("Rappi", [5])
    hack("Hack", [6])
    with connection() as c:
        pistas, _, _ = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert pistas == 0
    assert edges == []


def test_calendar_transitivo() -> None:
    # el vértice calendar es el consolidado; comparte correo con el gasto vía el crudo.
    fin = finance("Rappi", [7])
    cal = calendar("Reunión", [7])
    with connection() as c:
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert len(edges) == 1
    assert pair(edges[0]) == {("finance", fin), ("calendar", cal)}


def test_identidades_transitivo_via_mencion() -> None:
    fin = finance("Rappi", [8])
    p = person("Juan")
    mention(p, [8])
    with connection() as c:
        generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert len(edges) == 1
    assert pair(edges[0]) == {("finance", fin), ("identidades:person", p)}


def test_cooccurrence_persona_org_mismo_correo() -> None:
    p = person("Juan")
    o = org("Acme")
    mention(p, [9])
    mention(o, [9], kind="organizacion")
    with connection() as c:
        pistas, _, _ = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert pistas == 1
    assert len(edges) == 1
    e = edges[0]
    assert e.relation_type == "co-ocurrencia"
    assert pair(e) == {("identidades:person", p), ("identidades:org", o)}


def test_cooccurrence_misma_identidad_dos_menciones_no_edge() -> None:
    # dos menciones del MISMO correo que resuelven a la MISMA identidad → sin auto-enlace
    # (el set de `Ref` colapsa el vértice repetido; queda 1 < 2 vértices).
    p = person("Ana")
    mention(p, [10])
    mention(p, [10])
    with connection() as c:
        pistas, _, _ = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert pistas == 0
    assert edges == []


def test_cooccurrence_respeta_cap() -> None:
    # 3 vértices del mismo correo con cap=2 → ese mensaje se salta (la co-ocurrencia es ruido).
    a = person("A")
    b = person("B")
    d = person("C")
    mention(a, [11])
    mention(b, [11])
    mention(d, [11])
    with connection() as c:
        pistas, skipped, _ = generate_cooccurrence(c, 1, cap=2)
        edges = list_edges(c, 1)
    assert skipped == 1
    assert pistas == 0
    assert edges == []


def test_cooccurrence_producto_sobrevive_reconcile() -> None:
    # pista con vértice producto: el slug nuevo PROYECTA (NODE_SOURCES) → la poda de huérfanas
    # (reconcile_graph) NO la barre.
    prod = producto("Hearthstone")
    p = person("Rodion")
    mention(prod, [21], kind="producto")
    mention(p, [21])
    with connection() as c:
        pistas, _, _ = generate_cooccurrence(c, 1)
    assert pistas == 1
    with connection() as c:
        stats = reconcile_graph(c, 1)
        edges = list_edges(c, 1)
    assert stats.orphans_pruned == 0
    assert len(edges) == 1
    assert pair(edges[0]) == {("identidades:producto", prod), ("identidades:person", p)}


def test_idempotente() -> None:
    finance("Rappi", [5])
    hack("Hack", [5])
    with connection() as c:
        generate_cooccurrence(c, 1)
        n1 = len(list_edges(c, 1))
    with connection() as c:
        generate_cooccurrence(c, 1)
        n2 = len(list_edges(c, 1))
    assert n1 == n2 == 1


def test_cooccurrence_suprimida_por_confirmada_del_par() -> None:
    # la pista tx↔org duplicaría la contraparte confirmada del MISMO par → se suprime (la
    # conectividad ya la da la real, que pesa 1.0); los demás pares del mensaje SÍ emiten.
    o = org("Uber")
    fin = finance("Uber", [15], identity_id=o)
    mention(o, [15], kind="organizacion")
    h = hack("HackPago", [15])
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])  # paso 5: la contraparte real ANTES de generar
        pistas, _, redundant = generate_cooccurrence(c, 1)
        edges_inbox = list_edges(c, 1, producer="inbox")
    assert pistas == 2
    assert redundant == 0
    pares = [pair(e) for e in edges_inbox]
    assert {("finance", fin), ("identidades:org", o)} not in pares
    assert {("finance", fin), ("hackathones", h)} in pares
    assert {("identidades:org", o), ("hackathones", h)} in pares


def test_pista_redundante_preexistente_se_confirma() -> None:
    # gen1: tx sin identidad → pista tx↔org normal; luego la contraparte se resuelve y se teje →
    # gen2 confirma la pista redundante del par (historial, no DELETE) con decisión
    # regla/'redundante' y evidencia original intacta; gen3 es no-op.
    o = org("Uber")
    fin = finance("Uber", [16])
    mention(o, [16], kind="organizacion")
    with connection() as c:
        pistas1, _, _ = generate_cooccurrence(c, 1)
    assert pistas1 == 1
    run(
        "UPDATE mod_finance_consolidated SET counterparty_identity_id = :o WHERE id = :f",
        o=o,
        f=fin,
    )
    with connection() as c:
        weave_finance_consolidated(c, 1, [fin], [])  # paso 5: teje la contraparte resuelta
        _, _, redundant2 = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert redundant2 == 1
    assert len(edges) == 2  # la contraparte confirmada Y la pista (ahora confirmada)
    by_type = {e.relation_type: e for e in edges}
    assert by_type["contraparte"].verdict == "confirmed"
    cooc = by_type["co-ocurrencia"]
    assert cooc.verdict == "confirmed"
    assert cooc.evidence == "inbox:16"  # la procedencia original NO se pisa
    with connection() as c:
        dec = latest_decisions(c, 1, [cooc.id])[cooc.id]
    assert dec.verdict == "confirm" and dec.method == "regla" and dec.rule == "redundante"
    with connection() as c:
        _, _, redundant3 = generate_cooccurrence(c, 1)
        n = len(list_edges(c, 1))
    assert redundant3 == 0
    assert n == 2


def test_orientacion_inversa_tambien_suprime() -> None:
    # la confirmada es afiliado person→org; el par canónico de la pista sería org→person
    # (orden (slug, id)) → el par se compara sin orientación y la pista igual se suprime.
    p = person("Ana")
    o = org("Acme")
    link_person_org(p, o)
    mention(p, [17])
    mention(o, [17], kind="organizacion")
    with connection() as c:
        weave_afiliacion(c, 1, p)  # paso 5: la afiliación real ANTES de generar
        pistas, _, redundant = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert pistas == 0
    assert redundant == 0
    assert len(edges) == 1
    assert edges[0].relation_type == "afiliado"


def test_cooc_promovida_a_confirmed_no_se_toca() -> None:
    # una pista promovida a confirmed (cascada del partidor) NO se re-resuelve (el filtro
    # verdict=ambiguous la excluye) y su par tampoco se re-emite (ya está vouchado).
    finance("Rappi", [18])
    hack("HackX", [18])
    with connection() as c:
        generate_cooccurrence(c, 1)
        eid = list_edges(c, 1)[0].id
        resolve_edge(c, eid, verdict="confirmed", provenance="inferred")
    with connection() as c:
        pistas, _, redundant = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1)
    assert redundant == 0
    assert pistas == 0
    assert len(edges) == 1
    assert edges[0].verdict == "confirmed"
    assert edges[0].relation_type == "co-ocurrencia"


def test_sources_acumuladas_en_multiples_mensajes() -> None:
    # el MISMO par co-ocurre en dos mensajes: una sola arista (idempotente), `evidence` conserva
    # el primero, pero la PROCEDENCIA acumula AMBOS inbox ids en relation_edge_sources.
    finance("Steam", [21, 22])
    hack("HackSteam", [21, 22])
    with connection() as c:
        pistas, _, _ = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
    assert pistas == 1
    assert len(edges) == 1
    assert edges[0].evidence in {"inbox:21", "inbox:22"}
    with connection() as c:
        srcs = edge_sources(c, [edges[0].id])
    assert srcs == {edges[0].id: {21, 22}}
    with connection() as c:
        generate_cooccurrence(c, 1)
        srcs2 = edge_sources(c, [edges[0].id])
    assert srcs2 == {edges[0].id: {21, 22}}


def test_sources_siguen_creciendo_sobre_terminal() -> None:
    # la pista se resuelve (terminal) y DESPUÉS el par co-ocurre en un mensaje nuevo: no nace
    # arista nueva (terminal + confirmed_pairs) pero el inbox nuevo SÍ queda ligado.
    fin = finance("Steam", [23])
    h = hack("HackCeleste", [23])
    with connection() as c:
        generate_cooccurrence(c, 1)
        eid = list_edges(c, 1, producer="inbox")[0].id
        resolve_edge(c, eid, verdict="confirmed", provenance="inferred")
    run(
        "INSERT INTO mod_finance_transactions "
        "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, counterparty) "
        "VALUES (1, ARRAY[24]::bigint[], 'egreso', 50, 'COP', NOW(), 'Steam')"
    )
    run(
        "INSERT INTO mod_finance_transaction_links (user_id, transaction_id, consolidated_id) "
        "SELECT 1, t.id, :c FROM mod_finance_transactions t "
        "WHERE t.user_id = 1 AND t.source_inbox_ids = ARRAY[24]::bigint[]",
        c=fin,
    )
    run(
        "UPDATE mod_hackathones_events SET source_inbox_ids = ARRAY[23, 24]::bigint[] "
        "WHERE id = :h",
        h=h,
    )
    with connection() as c:
        pistas, _, _ = generate_cooccurrence(c, 1)
        edges = list_edges(c, 1, producer="inbox")
        srcs = edge_sources(c, [eid])
    assert pistas == 0  # par ya vouchado: no se re-emite ni cuenta
    assert len(edges) == 1 and edges[0].verdict == "confirmed"
    assert srcs == {eid: {23, 24}}
