"""Historial de aristas (`relation_edge_decisions` + `relation_edge_sources`): log append-only,
última decisión por arista, firma de evidencia determinista, procedencia idempotente, CASCADE.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.relations.decisions import (
    METHOD_LLM,
    METHOD_REGLA,
    VERDICT_CONFIRM,
    VERDICT_DEJAR,
    add_edge_sources,
    edge_sources,
    evidence_signature,
    latest_decisions,
    record_decision,
)
from memex.relations.edges import PRODUCER_INBOX, Ref, propose_edge

A = Ref("finance", 1)
B = Ref("calendar", 2)


def _edge() -> int:
    with connection() as c:
        return propose_edge(c, 1, A, B, producer=PRODUCER_INBOX, relation_type="co-ocurrencia")


def test_evidence_signature_determinista_e_independiente_del_orden() -> None:
    assert evidence_signature([3, 1, 2]) == evidence_signature([1, 2, 3])
    assert evidence_signature([1, 1, 2]) == evidence_signature([2, 1])  # set, no multiset
    assert evidence_signature([1, 2]) != evidence_signature([1, 3])
    assert len(evidence_signature([])) == 64


def test_record_y_latest_trae_la_ultima() -> None:
    eid = _edge()
    sig7 = evidence_signature([7])
    with connection() as c:
        record_decision(c, 1, eid, verdict=VERDICT_DEJAR, method=METHOD_LLM, evidence_sig=sig7)
        record_decision(
            c,
            1,
            eid,
            verdict=VERDICT_CONFIRM,
            method=METHOD_REGLA,
            rule="recibo",
            inbox_id=7,
            evidence_sig=evidence_signature([7, 9]),
        )
    with connection() as c:
        latest = latest_decisions(c, 1, [eid])
        total = c.execute(
            text("SELECT count(*) FROM relation_edge_decisions WHERE edge_id=:e"), {"e": eid}
        ).scalar()
    assert total == 2  # append-only: la primera no se pisa
    d = latest[eid]
    assert d.verdict == VERDICT_CONFIRM and d.method == METHOD_REGLA
    assert d.rule == "recibo" and d.inbox_id == 7
    assert d.evidence_sig == evidence_signature([9, 7])


def test_latest_decisions_vacio_y_sin_historial() -> None:
    eid = _edge()
    with connection() as c:
        assert latest_decisions(c, 1, []) == {}
        assert latest_decisions(c, 1, [eid]) == {}  # sin filas → no aparece


def test_verdict_y_method_invalidos() -> None:
    eid = _edge()
    with connection() as c:
        with pytest.raises(ValueError, match="verdict"):
            record_decision(c, 1, eid, verdict="maybe", method=METHOD_LLM, evidence_sig="x" * 64)
        with pytest.raises(ValueError, match="method"):
            record_decision(
                c, 1, eid, verdict=VERDICT_CONFIRM, method="oraculo", evidence_sig="x" * 64
            )


def test_sources_idempotentes_y_acumuladas() -> None:
    eid = _edge()
    with connection() as c:
        assert add_edge_sources(c, eid, [5, 7]) == 2
        assert add_edge_sources(c, eid, [7, 9]) == 1  # 7 ya estaba; 9 entra
        assert add_edge_sources(c, eid, []) == 0
    with connection() as c:
        assert edge_sources(c, [eid]) == {eid: {5, 7, 9}}
        assert edge_sources(c, []) == {}


def test_cascade_al_borrar_la_arista() -> None:
    eid = _edge()
    sig5 = evidence_signature([5])
    with connection() as c:
        record_decision(c, 1, eid, verdict=VERDICT_DEJAR, method=METHOD_LLM, evidence_sig=sig5)
        add_edge_sources(c, eid, [5])
        c.execute(text("DELETE FROM relation_edges WHERE id=:e"), {"e": eid})
    with connection() as c:
        decs = c.execute(
            text("SELECT count(*) FROM relation_edge_decisions WHERE edge_id=:e"), {"e": eid}
        ).scalar()
        srcs = c.execute(
            text("SELECT count(*) FROM relation_edge_sources WHERE edge_id=:e"), {"e": eid}
        ).scalar()
    assert decs == 0 and srcs == 0
