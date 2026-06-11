"""Resolver par-por-par (fase determinista): universo, componentes, modos --cluster/--vertex,
prefiltro por tipo de mensaje (recibo/bulk), memo `dejar` por evidence_sig, reporte de conflictos,
dry-run sin escrituras, idempotencia. La zona gris LLM se cubre en `test_resolve_llm.py`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from sqlalchemy import text

from memex.config import settings
from memex.db import connection
from memex.relations.decisions import (
    add_edge_sources,
    edge_sources,
    evidence_signature,
    latest_decisions,
    record_decision,
)
from memex.relations.edges import (
    PRODUCER_INBOX,
    RELTYPE_COOCURRENCIA,
    Ref,
    get_edge,
    list_edges,
    propose_edge,
    resolve_edge,
)
from memex.relations.resolve import ResolveStats, _parse_vertex, run_resolve


def _exec(sql: str, **p: Any) -> Any:
    with connection() as c:
        r = c.execute(text(sql), p)
        return r.scalar() if r.returns_rows else None


def _source(stype: str, name: str) -> int:
    return int(
        _exec(
            "INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id",
            n=name,
            t=stype,
        )
    )


def _inbox(source_id: int, ext: str, payload: dict[str, Any] | None = None) -> int:
    return int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :sid, :ext, NOW(), CAST(:pl AS JSONB)) RETURNING id",
            sid=source_id,
            ext=ext,
            pl=json.dumps(payload or {}),
        )
    )


def _person(name: str) -> Ref:
    pid = _exec(
        "INSERT INTO mod_identidades (user_id, kind, display_name) "
        "VALUES (1, 'persona', :n) RETURNING id",
        n=name,
    )
    return Ref("identidades:person", int(pid))


def _pista(a: Ref, b: Ref, sources: list[int]) -> int:
    """Una pista de co-ocurrencia con su procedencia ya ligada (como la deja `build`)."""
    with connection() as c:
        eid = propose_edge(
            c,
            1,
            a,
            b,
            producer=PRODUCER_INBOX,
            relation_type=RELTYPE_COOCURRENCIA,
            evidence=f"inbox:{sources[0]}" if sources else "",
        )
        add_edge_sources(c, eid, sources)
    return eid


def _finance_tx(inbox_ids: list[int]) -> None:
    """Una transacción cruda de finanzas: marca sus mensajes como RECIBO para el prefiltro."""
    _exec(
        "INSERT INTO mod_finance_transactions "
        "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, counterparty) "
        "VALUES (1, :ids, 'egreso', 100, 'COP', NOW(), 'X')",
        ids=inbox_ids,
    )


_BULK_PAYLOAD = {"list_unsubscribe": "<mailto:unsub@x.com>", "subject": "Promos"}


def _run(**kw: Any) -> ResolveStats:
    return asyncio.run(run_resolve(1, no_llm=True, **kw))


# --- universo + grupos ------------------------------------------------------------------ #


def test_universo_excluye_canal_terminales_y_no_cooc() -> None:
    src = _source("imap", "mail-u")
    m = _inbox(src, "u1")
    a, b, c = _person("A"), _person("B"), _person("C")
    _pista(a, b, [m])  # SÍ entra
    with connection() as conn:
        # terminal: fuera del universo
        eid = propose_edge(
            conn, 1, a, c, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA
        )
        resolve_edge(conn, eid, status="confirmed")
        # pista que toca un canal: fuera
        propose_edge(
            conn, 1, b, Ref("canal", 9), producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA
        )
        # pista de otro relation_type: fuera
        propose_edge(conn, 1, b, c, producer=PRODUCER_INBOX, relation_type="otra")
    stats = _run(dry_run=True)
    assert stats.pairs == 1


def test_componentes_chicas_primero_y_limit() -> None:
    src = _source("imap", "mail-c")
    m1, m2 = _inbox(src, "c1"), _inbox(src, "c2")
    # componente grande (3 vértices, 2 pares) y chica (2 vértices, 1 par)
    a, b, c = _person("A"), _person("B"), _person("C")
    d, e = _person("D"), _person("E")
    _pista(a, b, [m1])
    _pista(b, c, [m1])
    _pista(d, e, [m2])
    stats = _run(dry_run=True, limit=1)
    assert stats.groups == 1
    assert stats.pairs == 1  # la componente CHICA (d,e) va primero


def test_modo_vertex_toma_su_componente() -> None:
    src = _source("imap", "mail-v")
    m1, m2 = _inbox(src, "v1"), _inbox(src, "v2")
    a, b = _person("A"), _person("B")
    c, d = _person("C"), _person("D")
    _pista(a, b, [m1])
    _pista(c, d, [m2])
    stats = _run(dry_run=True, vertex=a)
    assert stats.groups == 1 and stats.pairs == 1
    # un vértice sin pistas → 0 grupos
    stats2 = _run(dry_run=True, vertex=_person("Z"))
    assert stats2.groups == 0 and stats2.pairs == 0


def test_modo_cluster_solo_pares_internos() -> None:
    src = _source("imap", "mail-k")
    m = _inbox(src, "k1")
    a, b, c = _person("A"), _person("B"), _person("C")
    _pista(a, b, [m])  # interno
    _pista(b, c, [m])  # c NO es miembro → fuera
    cid = _exec(
        "INSERT INTO relation_clusters (user_id, status, signature, blob_signature, member_count) "
        "VALUES (1, 'confirmed', 'x', 'x', 2) RETURNING id"
    )
    for r in (a, b):
        _exec(
            "INSERT INTO relation_cluster_members (user_id, cluster_id, member_slug, member_id) "
            "VALUES (1, :c, :s, :i)",
            c=cid,
            s=r.slug,
            i=r.id,
        )
    stats = _run(dry_run=True, cluster_id=int(cid))
    assert stats.groups == 1 and stats.pairs == 1


# --- prefiltro --------------------------------------------------------------------------- #


def test_recibo_confirma_con_historial() -> None:
    src = _source("imap", "mail-r")
    m = _inbox(src, "r1", {"subject": "Tu recibo de compra"})
    _finance_tx([m])
    a, b = _person("Steam"), _person("Celeste")
    eid = _pista(a, b, [m])
    stats = _run()
    assert stats.confirmed_recibo == 1 and stats.gray_pairs == 0
    with connection() as c:
        edge = get_edge(c, 1, a, b, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA)
        dec = latest_decisions(c, 1, [eid])[eid]
        srcs = edge_sources(c, [eid])
    assert edge is not None and edge.status == "confirmed"
    assert edge.evidence == f"inbox:{m}"  # la procedencia original NO se pisa
    assert dec.verdict == "confirm" and dec.method == "regla" and dec.rule == "recibo"
    assert dec.inbox_id == m and dec.evidence_sig == evidence_signature([m])
    assert srcs == {eid: {m}}  # las sources sobreviven al veredicto


def test_bulk_no_decide_por_default() -> None:
    src = _source("imap", "mail-b")
    m = _inbox(src, "b1", _BULK_PAYLOAD)
    a, b = _person("Steam"), _person("Hollow Knight")
    eid = _pista(a, b, [m])
    stats = _run()
    assert stats.gray_pairs == 1 and stats.rejected_bulk == 0
    with connection() as c:
        edges = list_edges(c, 1, status="pista")
        assert latest_decisions(c, 1, [eid]) == {}  # sin memo: queda pendiente real
    assert len(edges) == 1


def test_bulk_rechaza_con_knob(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "resolve_reject_bulk", True)
    src = _source("imap", "mail-bk")
    m = _inbox(src, "bk1", _BULK_PAYLOAD)
    a, b = _person("Steam"), _person("Cuphead")
    eid = _pista(a, b, [m])
    stats = _run()
    assert stats.rejected_bulk == 1 and stats.gray_pairs == 0
    with connection() as c:
        dec = latest_decisions(c, 1, [eid])[eid]
        edge = get_edge(c, 1, a, b, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA)
    assert edge is not None and edge.status == "rejected"
    assert dec.verdict == "reject" and dec.rule == "bulk"


def test_bulk_via_classifications_y_recibo_gana() -> None:
    # tier persistido (sin headers en payload) también etiqueta BULK; pero si OTRO mensaje del
    # par es recibo, el recibo gana (algún RECIBO → confirm).
    src = _source("imap", "mail-bc")
    m1 = _inbox(src, "bc1")
    m2 = _inbox(src, "bc2")
    _exec(
        "INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :m, 'blacklist')",
        m=m1,
    )
    _finance_tx([m2])
    a, b = _person("Steam"), _person("Stardew")
    _pista(a, b, [m1, m2])
    stats = _run()
    assert stats.confirmed_recibo == 1


def test_chat_es_zona_gris() -> None:
    src = _source("telegram", "tg-g")
    m = _inbox(src, "g1", {"text": "hola", "chat_id": "55"})
    a, b = _person("Ana"), _person("Beto")
    _pista(a, b, [m])
    stats = _run()
    assert stats.gray_pairs == 1 and stats.gray_messages == 1


# --- memo dejar + staleness --------------------------------------------------------------- #


def test_sin_evidencia_memo_y_reevaluacion_con_sig_nueva() -> None:
    src = _source("imap", "mail-se")
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [])  # sin mensajes de evidencia (procedencia perdida)
    stats = _run()
    assert stats.sin_evidencia == 1
    # segunda corrida: el memo con la MISMA sig lo salta
    stats2 = _run()
    assert stats2.skipped_dejar == 1 and stats2.pairs == 0
    # aparece evidencia nueva (un recibo) → la sig cambia → se re-evalúa y confirma
    m = _inbox(src, "se1")
    _finance_tx([m])
    with connection() as c:
        add_edge_sources(c, eid, [m])
    stats3 = _run()
    assert stats3.skipped_dejar == 0 and stats3.confirmed_recibo == 1


def test_conflicto_recibo_sobre_rechazada_se_reporta() -> None:
    src = _source("imap", "mail-cf")
    m1, m2 = _inbox(src, "cf1"), _inbox(src, "cf2")
    a, b = _person("Steam"), _person("Hades")
    eid = _pista(a, b, [m1])
    with connection() as c:
        resolve_edge(c, eid, status="rejected")
        record_decision(
            c,
            1,
            eid,
            verdict="reject",
            method="llm",
            evidence_sig=evidence_signature([m1]),
        )
        add_edge_sources(c, eid, [m2])  # evidencia nueva DESPUÉS del veredicto
    _finance_tx([m2])  # y es un RECIBO → conflicto fuerte
    stats = _run(dry_run=True)
    assert stats.stale_conflicts == 1
    assert stats.stale_recibo_conflicts == 1


# --- dry-run + idempotencia ----------------------------------------------------------------- #


def test_dry_run_no_escribe_nada() -> None:
    src = _source("imap", "mail-d")
    m = _inbox(src, "d1")
    _finance_tx([m])
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [m])
    stats = _run(dry_run=True)
    assert stats.confirmed_recibo == 1  # proyección
    with connection() as c:
        edge = get_edge(c, 1, a, b, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA)
        assert latest_decisions(c, 1, [eid]) == {}
    assert edge is not None and edge.status == "pista"  # NADA se escribió


def test_dry_run_estima_llamadas_con_budget() -> None:
    src = _source("imap", "mail-e")
    m1, m2 = _inbox(src, "e1", _BULK_PAYLOAD), _inbox(src, "e2", _BULK_PAYLOAD)
    a, b, c = _person("A"), _person("B"), _person("C")
    _pista(a, b, [m1])
    _pista(b, c, [m2])
    stats = _run(dry_run=True)
    assert stats.gray_messages == 2 and stats.estimated_calls == 2
    stats2 = _run(dry_run=True, max_llm_calls=1)
    assert stats2.estimated_calls == 1


def test_idempotente_no_llm() -> None:
    src = _source("imap", "mail-i")
    m = _inbox(src, "i1")
    _finance_tx([m])
    a, b = _person("A"), _person("B")
    _pista(a, b, [m])
    stats1 = _run()
    assert stats1.confirmed_recibo == 1
    stats2 = _run()  # el terminal salió del universo
    assert stats2.pairs == 0 and stats2.confirmed_recibo == 0


# --- fallback de evidencia + parseo de vértice ---------------------------------------------- #


def test_fallback_interseccion_de_provenance() -> None:
    # pista SIN filas en relation_edge_sources (pre-backfill): la evidencia sale de la
    # intersección de la provenance de ambos extremos (mentions → source_inbox_ids).
    src = _source("imap", "mail-f")
    m = _inbox(src, "f1")
    _finance_tx([m])
    a, b = _person("A"), _person("B")
    for r in (a, b):
        _exec(
            "INSERT INTO mod_identidades_mentions "
            "(user_id, source_inbox_ids, mentioned_name, resolved_identity_id) "
            "VALUES (1, :ids, 'x', :i)",
            ids=[m],
            i=r.id,
        )
    with connection() as c:
        propose_edge(
            c, 1, a, b, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA
        )  # sin add_edge_sources
    stats = _run()
    assert stats.sin_evidencia == 0
    assert stats.confirmed_recibo == 1


def test_parse_vertex() -> None:
    assert _parse_vertex("identidades:person:7") == Ref("identidades:person", 7)
    assert _parse_vertex("finance:12") == Ref("finance", 12)
    with pytest.raises(ValueError):
        _parse_vertex("sin-id")
    with pytest.raises(ValueError):
        _parse_vertex(":7")
