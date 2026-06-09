"""Repositorio `relation_edges` (Fase 0 del grafo): idempotencia por productor, niveles
pista/confirmed, resolución monótona, anti self-loop. Aristas = referencias (slug,id), sin
ontología.
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
    list_pistas,
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
    # pista del inbox + real del LLM sobre el MISMO par conviven (el productor está en la UNIQUE)
    id_inbox = _propose(producer=PRODUCER_INBOX)  # pista
    id_llm = _propose(producer=PRODUCER_LLM, status="confirmed")  # real
    assert id_inbox != id_llm
    assert _count() == 2


def test_producer_es_obligatorio() -> None:
    with pytest.raises(ValueError, match="producer"):
        _propose(producer="")


def test_status_por_defecto_es_pista() -> None:
    # una arista de co-ocurrencia del inbox nace como PISTA (señal sin vouchar)
    _propose(producer=PRODUCER_INBOX)
    with connection() as c:
        edge = get_edge(c, 1, A, B, producer=PRODUCER_INBOX)
    assert edge is not None
    assert edge.status == "pista"


def test_resolve_es_monotonico() -> None:
    eid = _propose(producer=PRODUCER_INBOX)  # nace pista
    with connection() as c:
        assert resolve_edge(c, eid, status="confirmed") is True
    # una arista terminal NO se re-evalúa → noop
    with connection() as c:
        assert resolve_edge(c, eid, status="rejected") is False
    with connection() as c:
        edge = get_edge(c, 1, A, B, producer=PRODUCER_INBOX)  # resolver NO reescribe el productor
    assert edge is not None
    assert edge.status == "confirmed"


def test_resolve_conserva_productor_y_no_colisiona() -> None:
    # pista del inbox + arista confirmed del LLM (overflow de identidades) sobre el MISMO par y
    # relation_type → coexisten por productor distinto. Resolver la pista NO reescribe su productor
    # (lo haría chocaría con la UNIQUE de la arista llm). Estampa `evidence`.
    eid = _propose(producer=PRODUCER_INBOX, relation_type="co-ocurrencia")  # pista inbox
    _propose(producer=PRODUCER_LLM, relation_type="co-ocurrencia", status="confirmed")  # llm
    with connection() as c:
        assert resolve_edge(c, eid, status="confirmed", evidence="cluster:7") is True
    with connection() as c:
        pista = get_edge(c, 1, A, B, producer=PRODUCER_INBOX, relation_type="co-ocurrencia")
        llm = get_edge(c, 1, A, B, producer=PRODUCER_LLM, relation_type="co-ocurrencia")
    assert pista is not None and pista.status == "confirmed" and pista.evidence == "cluster:7"
    assert llm is not None and llm.status == "confirmed"  # ambas coexisten, sin IntegrityError


def test_anti_self_loop() -> None:
    # un vértice no se enlaza consigo mismo (CHECK en la DB)
    with pytest.raises(IntegrityError):
        _propose(src=A, dst=A, producer=PRODUCER_INBOX)


def test_list_pistas_solo_trae_pistas() -> None:
    _propose(producer=PRODUCER_INBOX)  # pista (por defecto)
    _propose(
        src=Ref("finance", 3),
        dst=Ref("calendar", 4),
        producer=PRODUCER_LLM,
        status="confirmed",  # real
    )
    with connection() as c:
        pistas = list_pistas(c, 1)
    assert len(pistas) == 1
    assert pistas[0].status == "pista"
