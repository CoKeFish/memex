"""Paso de relaciones deterministas (Fase 2): pistas de co-ocurrencia (mismo correo, directo y
transitivo) + afiliación real persona↔org; idempotencia; tope de fan-out.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.relations.deterministic import build_relations
from memex.relations.edges import list_edges


def _exec(sql: str, **params: Any) -> Any:
    with connection() as c:
        result = c.execute(text(sql), params)
        return result.scalar() if result.returns_rows else None


def _finance(merchant: str, inbox_ids: list[int]) -> int:
    return int(
        _exec(
            "INSERT INTO mod_finance_expenses "
            "(user_id, source_inbox_ids, amount, currency, merchant) "
            "VALUES (1, :ids, 100, 'COP', :m) RETURNING id",
            ids=inbox_ids,
            m=merchant,
        )
    )


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
            "INSERT INTO mod_identidades_persons (user_id, display_name) "
            "VALUES (1, :n) RETURNING id",
            n=name,
        )
    )


def _org(name: str) -> int:
    return int(
        _exec(
            "INSERT INTO mod_identidades_orgs (user_id, name) VALUES (1, :n) RETURNING id", n=name
        )
    )


def _link_person_org(person_id: int, org_id: int) -> None:
    _exec(
        "INSERT INTO mod_identidades_person_orgs (user_id, person_id, org_id) VALUES (1, :p, :o)",
        p=person_id,
        o=org_id,
    )


def _mention(person_id: int, inbox_ids: list[int]) -> None:
    _exec(
        "INSERT INTO mod_identidades_mentions "
        "(user_id, source_inbox_ids, mentioned_name, resolved_person_id) "
        "VALUES (1, :ids, 'X', :p)",
        ids=inbox_ids,
        p=person_id,
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
