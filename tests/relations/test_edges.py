"""Repositorio `relation_edges` (Fase 0 del grafo): idempotencia por productor, hechos vs.
inferencias, resolución monótona, anti self-loop. Aristas = referencias (slug,id), sin ontología.
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
    Ref,
    get_edge,
    list_pending,
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
    # dos procedencias del MISMO par son aristas independientes (el productor está en la UNIQUE)
    id_inbox = _propose(producer=PRODUCER_INBOX)
    id_llm = _propose(producer=PRODUCER_LLM, status="pending")
    assert id_inbox != id_llm
    assert _count() == 2


def test_producer_es_obligatorio() -> None:
    with pytest.raises(ValueError, match="producer"):
        _propose(producer="")


def test_status_none_es_hecho_determinista() -> None:
    # una arista de inbox nace como HECHO (sin cola de revisión): status NULL
    _propose(producer=PRODUCER_INBOX)
    with connection() as c:
        edge = get_edge(c, 1, A, B, producer=PRODUCER_INBOX)
    assert edge is not None
    assert edge.status is None


def test_resolve_es_monotonico() -> None:
    eid = _propose(producer=PRODUCER_LLM, status="pending")
    with connection() as c:
        assert resolve_edge(c, eid, status="confirmed", producer=PRODUCER_LLM) is True
    # una arista terminal NO se re-evalúa → noop
    with connection() as c:
        assert resolve_edge(c, eid, status="rejected", producer=PRODUCER_LLM) is False
    with connection() as c:
        edge = get_edge(c, 1, A, B, producer=PRODUCER_LLM)
    assert edge is not None
    assert edge.status == "confirmed"


def test_anti_self_loop() -> None:
    # un vértice no se enlaza consigo mismo (CHECK en la DB)
    with pytest.raises(IntegrityError):
        _propose(src=A, dst=A, producer=PRODUCER_INBOX)


def test_list_pending_solo_trae_inferencias() -> None:
    _propose(producer=PRODUCER_INBOX)  # hecho determinista (status None)
    _propose(
        src=Ref("finance", 3),
        dst=Ref("calendar", 4),
        producer=PRODUCER_LLM,
        status="pending",
    )
    with connection() as c:
        pend = list_pending(c, 1)
    assert len(pend) == 1
    assert pend[0].status == "pending"
