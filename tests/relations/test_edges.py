"""Repositorio `relation_edges` (Fase 0 del grafo): idempotencia por productor, DOS EJES
(procedencia por veredicto), resolución monótona (confirm gana), anti self-loop. Aristas =
referencias (slug,id), sin ontología.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from memex.db import connection
from memex.relations.edges import (
    PRODUCER_INBOX,
    PRODUCER_LLM,
    PROVENANCE_EXTRACTED,
    PROVENANCE_INFERRED,
    VERDICT_AMBIGUOUS,
    VERDICT_CONFIRMED,
    VERDICT_REJECTED,
    Ref,
    canonical_label,
    edges_touching,
    get_edge,
    list_ambiguous,
    mark_vertices_dirty,
    propose_edge,
    resolve_edge,
)

A = Ref("finance", 1)
B = Ref("calendar", 2)


def _count() -> int:
    with connection() as c:
        sql = text("SELECT count(*) FROM relation_edges WHERE user_id=1")
        return int(c.execute(sql).scalar() or 0)


def _propose(src: Ref = A, dst: Ref = B, **kw: Any) -> int:
    with connection() as c:
        return propose_edge(c, 1, src, dst, **kw)


def test_propose_es_idempotente_por_productor() -> None:
    # el MISMO productor re-corriendo no duplica la arista
    id1 = _propose(producer=PRODUCER_INBOX)
    id2 = _propose(producer=PRODUCER_INBOX)
    assert id1 == id2
    assert _count() == 1


def test_distinto_productor_coexiste() -> None:
    # co-ocurrencia ambigua del inbox + real del LLM sobre el MISMO par conviven (productor único)
    id_inbox = _propose(producer=PRODUCER_INBOX)  # extracted+ambiguous
    id_llm = _propose(
        producer=PRODUCER_LLM, verdict=VERDICT_CONFIRMED, provenance=PROVENANCE_INFERRED
    )
    assert id_inbox != id_llm
    assert _count() == 2


def test_producer_es_obligatorio() -> None:
    with pytest.raises(ValueError, match="producer"):
        _propose(producer="")


def test_ejes_por_defecto_extracted_ambiguous() -> None:
    # nace extracted (la co-aparición es un hecho) + ambiguous (la relación, sin juzgar)
    _propose(producer=PRODUCER_INBOX)
    with connection() as c:
        edge = get_edge(c, 1, A, B, producer=PRODUCER_INBOX)
    assert edge is not None
    assert edge.provenance == PROVENANCE_EXTRACTED
    assert edge.verdict == VERDICT_AMBIGUOUS
    assert edge.label == "AMBIGUOUS"


def test_resolve_es_monotonico() -> None:
    eid = _propose(producer=PRODUCER_INBOX)  # nace ambiguous
    with connection() as c:
        assert (
            resolve_edge(c, eid, verdict=VERDICT_CONFIRMED, provenance=PROVENANCE_INFERRED) is True
        )
    # un confirmed NO se rebaja a rejected → noop
    with connection() as c:
        assert (
            resolve_edge(c, eid, verdict=VERDICT_REJECTED, provenance=PROVENANCE_INFERRED) is False
        )
    with connection() as c:
        edge = get_edge(c, 1, A, B, producer=PRODUCER_INBOX)  # resolver NO reescribe el productor
    assert edge is not None
    assert edge.verdict == VERDICT_CONFIRMED


def test_confirm_gana_sobre_reject() -> None:
    # un confirm POSTERIOR upgradea un rejected (confirm gana); el revés no.
    eid = _propose(producer=PRODUCER_INBOX)  # ambiguous
    with connection() as c:
        assert (
            resolve_edge(c, eid, verdict=VERDICT_REJECTED, provenance=PROVENANCE_INFERRED) is True
        )
    with connection() as c:
        assert (
            resolve_edge(
                c, eid, verdict=VERDICT_CONFIRMED, provenance=PROVENANCE_INFERRED, relation="sí"
            )
            is True
        )
    with connection() as c:
        edge = get_edge(c, 1, A, B, producer=PRODUCER_INBOX)
    assert edge is not None
    assert edge.verdict == VERDICT_CONFIRMED and edge.relation == "sí"


def test_resolve_conserva_productor_y_no_colisiona() -> None:
    # ambigua del inbox + arista confirmed del LLM (overflow de identidades) sobre el MISMO par y
    # relation_type → coexisten por productor distinto. Resolver NO reescribe el productor.
    eid = _propose(producer=PRODUCER_INBOX, relation_type="co-ocurrencia")
    _propose(
        producer=PRODUCER_LLM,
        relation_type="co-ocurrencia",
        verdict=VERDICT_CONFIRMED,
        provenance=PROVENANCE_INFERRED,
    )
    with connection() as c:
        assert (
            resolve_edge(
                c,
                eid,
                verdict=VERDICT_CONFIRMED,
                provenance=PROVENANCE_INFERRED,
                evidence="cluster:7",
            )
            is True
        )
    with connection() as c:
        ambigua = get_edge(c, 1, A, B, producer=PRODUCER_INBOX, relation_type="co-ocurrencia")
        llm = get_edge(c, 1, A, B, producer=PRODUCER_LLM, relation_type="co-ocurrencia")
    assert ambigua is not None and ambigua.verdict == VERDICT_CONFIRMED
    assert ambigua.evidence == "cluster:7"
    assert llm is not None and llm.verdict == VERDICT_CONFIRMED  # coexisten, sin IntegrityError


def test_anti_self_loop() -> None:
    # un vértice no se enlaza consigo mismo (CHECK en la DB)
    with pytest.raises(IntegrityError):
        _propose(src=A, dst=A, producer=PRODUCER_INBOX)


def test_list_ambiguous_solo_trae_ambiguas() -> None:
    _propose(producer=PRODUCER_INBOX)  # ambiguous (por defecto)
    _propose(
        src=Ref("finance", 3),
        dst=Ref("calendar", 4),
        producer=PRODUCER_LLM,
        verdict=VERDICT_CONFIRMED,
        provenance=PROVENANCE_INFERRED,
    )
    with connection() as c:
        ambiguas = list_ambiguous(c, 1)
    assert len(ambiguas) == 1
    assert ambiguas[0].verdict == VERDICT_AMBIGUOUS


def test_edges_touching_ambos_extremos_y_verdict() -> None:
    # A está como SRC en una arista y como DST en otra → edges_touching(A) trae ambas.
    _propose(src=A, dst=B, producer=PRODUCER_INBOX)  # A es src (ambiguous)
    _propose(
        src=Ref("finance", 9),
        dst=A,
        producer=PRODUCER_LLM,
        verdict=VERDICT_CONFIRMED,
        provenance=PROVENANCE_INFERRED,
    )  # A es dst
    _propose(src=Ref("finance", 9), dst=B, producer=PRODUCER_INBOX)  # no toca A
    with connection() as c:
        touching = edges_touching(c, 1, A)
        confirmed = edges_touching(c, 1, A, verdict=VERDICT_CONFIRMED)
    assert len(touching) == 2  # ambas direcciones, ignora la que no toca A
    assert {e.verdict for e in touching} == {VERDICT_AMBIGUOUS, VERDICT_CONFIRMED}
    assert len(confirmed) == 1 and confirmed[0].verdict == VERDICT_CONFIRMED


def test_canonical_label() -> None:
    assert canonical_label(PROVENANCE_EXTRACTED, VERDICT_CONFIRMED) == "EXTRACTED"
    assert canonical_label(PROVENANCE_INFERRED, VERDICT_CONFIRMED) == "INFERRED"
    assert canonical_label(PROVENANCE_INFERRED, VERDICT_REJECTED) == "INFERRED REJECTED"
    assert canonical_label(PROVENANCE_INFERRED, VERDICT_AMBIGUOUS) == "AMBIGUOUS (inferred)"
    assert canonical_label(PROVENANCE_EXTRACTED, VERDICT_AMBIGUOUS) == "AMBIGUOUS"


def test_mark_vertices_dirty_upsert() -> None:
    # groundwork incremental: marca vértices dirty, idempotente por (user, slug, id).
    with connection() as c:
        mark_vertices_dirty(c, 1, [A, B, A])  # A repetido: dedupe
    with connection() as c:
        n = c.execute(
            text("SELECT count(*) FROM relation_vertex_state WHERE user_id=1 AND dirty")
        ).scalar()
    assert n == 2
