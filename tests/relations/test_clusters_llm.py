"""PARTIDOR LLM de cúmulos (LLMClient FALSO, sin red). Cubre: parseo ultra-defensivo
(`parse_partition`); el worker `run_cluster_partition` (crea hijos por contexto, promueve pistas
intra-grupo, separa contextos, deja inter-grupo/ruido como pista, gate de confianza, memo de ruido,
preserva identidad al re-particionar, skip por tamaño, `LLMQuotaError` propaga, no toca det.).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Connection

from memex.config import settings
from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.llm.client import LLMQuotaError
from memex.relations.cluster_store import create_child, insert_candidate
from memex.relations.clustering import CandidateCluster, cluster_signature
from memex.relations.clusters_llm import parse_partition, run_cluster_partition
from memex.relations.edges import (
    PRODUCER_IDENTIDADES,
    PRODUCER_INBOX,
    Ref,
    list_edges,
    propose_edge,
)


class FakeLLM:
    """Devuelve siempre el mismo `content`; cuenta llamadas. Cumple el Protocol LLMClient."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls += 1
        return LLMResult(
            content=self.content,
            model="fake",
            usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


class QuotaLLM:
    """LLMClient falso que agota cuota en cada llamada."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        raise LLMQuotaError(402, "sin saldo")


# --- siembra (committed: el worker abre sus propias conexiones) -------------------- #


def _person(conn: Connection, name: str) -> Ref:
    pid = conn.execute(
        text(
            "INSERT INTO mod_identidades (user_id, kind, display_name) "
            "VALUES (1, 'persona', :n) RETURNING id"
        ),
        {"n": name},
    ).scalar_one()
    return Ref("identidades:person", int(pid))


def _seed_candidate(conn: Connection, members: list[Ref]) -> int:
    """Un blob `candidate` (lo que detección produce) usando la vía de producción."""
    ordered = tuple(sorted(members, key=lambda r: (r.slug, r.id)))
    cc = CandidateCluster(ordered, cluster_signature(ordered), False)
    cid = insert_candidate(conn, 1, cc)
    assert cid is not None
    return cid


def _seed_confirmed_child(conn: Connection, members: list[Ref], *, name: str, blob_sig: str) -> int:
    """Un hijo `confirmed` existente (para los tests de re-partición/identidad)."""
    return create_child(
        conn,
        1,
        blob_sig,
        frozenset(members),
        name=name,
        description="",
        confidence=0.9,
        has_confirmed_edge=False,
    )


def _pista(conn: Connection, a: Ref, b: Ref) -> int:
    """Una pista de co-ocurrencia (producer=inbox) entre dos miembros — candidata a promoción."""
    return propose_edge(
        conn, 1, a, b, producer=PRODUCER_INBOX, relation_type="co-ocurrencia", evidence="inbox:99"
    )


def _edges_by_id() -> dict[int, Any]:
    with connection() as c:
        return {e.id: e for e in list_edges(c, 1)}


def _confirmed_rows() -> list[dict[str, Any]]:
    with connection() as c:
        return [
            dict(r)
            for r in c.execute(
                text(
                    "SELECT id, status, name, member_count, blob_signature FROM relation_clusters "
                    "WHERE user_id = 1 AND status = 'confirmed' ORDER BY id"
                )
            ).mappings()
        ]


# --- parse_partition (puro) -------------------------------------------------------- #


def test_parse_partition_dos_grupos_orden_canonico() -> None:
    r = parse_partition(
        '{"groups":[{"members":[4,3],"name":"B","description":"d2","confidence":0.8},'
        '{"members":[2,1],"name":"A","description":"d1","confidence":0.9}]}',
        4,
    )
    assert r.valid is True
    assert [(g.members, g.name) for g in r.groups] == [((1, 2), "A"), ((3, 4), "B")]


def test_parse_partition_basura_es_invalida() -> None:
    assert parse_partition("no soy json", 3).valid is False
    assert parse_partition('{"sin":"groups"}', 3).valid is False
    assert parse_partition('{"groups":"x"}', 3).valid is False


def test_parse_partition_vacio_es_ruido() -> None:
    r = parse_partition('{"groups":[]}', 3)
    assert r.valid is True and r.groups == ()


def test_parse_partition_asigna_al_primero_y_descarta_singleton() -> None:
    r = parse_partition(
        '{"groups":[{"members":[1,2],"name":"A","description":"","confidence":0.9},'
        '{"members":[2,3],"name":"B","description":"","confidence":0.9}]}',
        3,
    )
    assert [g.members for g in r.groups] == [(1, 2)]


def test_parse_partition_filtra_ids_y_clampa_confianza() -> None:
    r = parse_partition(
        '{"groups":[{"members":[0,9,1,2,true],"name":"A","description":"","confidence":2}]}', 3
    )
    assert len(r.groups) == 1
    assert r.groups[0].members == (1, 2) and r.groups[0].confidence == 1.0


def test_parse_partition_rejected_edges_canoniza_dedup_e_ignora_basura() -> None:
    r = parse_partition(
        '{"groups":[{"members":[1,2],"name":"A","description":"","confidence":0.9}],'
        '"rejected_edges":["2-1","1-2","3-3","9-1","a-b",5]}',
        3,
    )
    assert r.valid is True
    assert r.rejected_pairs == ((1, 2),)  # canónico, dedup; fuera-de-rango/self/no-string ignorados
    # Campo ausente → tupla vacía (comportamiento previo intacto).
    assert parse_partition('{"groups":[]}', 3).rejected_pairs == ()


# --- worker: run_cluster_partition (LLM falso) ------------------------------------- #

_G3 = '{"groups":[{"members":[1,2,3],"name":"Ctx","description":"d","confidence":0.9}]}'


@pytest.mark.asyncio
async def test_particion_crea_hijo_y_promueve_pistas() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        _seed_candidate(c, p)
        e01 = _pista(c, p[0], p[1])
        e12 = _pista(c, p[1], p[2])
    stats = await run_cluster_partition(1, client=FakeLLM(_G3))
    assert (stats.blobs, stats.groups, stats.created, stats.promoted) == (1, 1, 1, 2)
    rows = _confirmed_rows()
    assert len(rows) == 1 and rows[0]["name"] == "Ctx" and rows[0]["member_count"] == 3
    edges = _edges_by_id()
    assert edges[e01].status == "confirmed" and edges[e12].status == "confirmed"


@pytest.mark.asyncio
async def test_particion_separa_dos_contextos() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(4)]
        _seed_candidate(c, p)
        e01 = _pista(c, p[0], p[1])  # intra-grupo A → promueve
        e23 = _pista(c, p[2], p[3])  # intra-grupo B → promueve
        e12 = _pista(c, p[1], p[2])  # INTER-grupo → queda pista (no se mata)
    fake = FakeLLM(
        '{"groups":[{"members":[1,2],"name":"A","description":"","confidence":0.9},'
        '{"members":[3,4],"name":"B","description":"","confidence":0.9}]}'
    )
    stats = await run_cluster_partition(1, client=fake)
    assert (stats.blobs, stats.created, stats.groups, stats.promoted) == (1, 2, 2, 2)
    assert len(_confirmed_rows()) == 2
    edges = _edges_by_id()
    assert edges[e01].status == "confirmed" and edges[e23].status == "confirmed"
    assert edges[e12].status == "pista"  # inter-contexto → sin matar


@pytest.mark.asyncio
async def test_grupo_bajo_umbral_no_se_confirma() -> None:
    # confianza < cluster_partition_min_confidence (0.75) → no es contexto confiado → ruido.
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        _seed_candidate(c, p)
        e01 = _pista(c, p[0], p[1])
    fake = FakeLLM('{"groups":[{"members":[1,2,3],"name":"X","description":"","confidence":0.5}]}')
    stats = await run_cluster_partition(1, client=fake)
    assert (stats.created, stats.groups, stats.rejected) == (0, 0, 1)
    assert _edges_by_id()[e01].status == "pista"  # ni se promovió ni se mató


@pytest.mark.asyncio
async def test_particion_vacia_es_memo_y_no_mata_pistas() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        cid = _seed_candidate(c, p)
        e01 = _pista(c, p[0], p[1])
    stats = await run_cluster_partition(1, client=FakeLLM('{"groups":[]}'))
    assert (stats.blobs, stats.groups, stats.rejected) == (1, 0, 1)
    with connection() as c:
        st = c.execute(
            text("SELECT status FROM relation_clusters WHERE id = :c"), {"c": cid}
        ).scalar_one()
    assert st == "rejected"  # memo
    assert _edges_by_id()[e01].status == "pista"  # NO-destructivo


@pytest.mark.asyncio
async def test_json_basura_deja_el_candidate() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        cid = _seed_candidate(c, p)
    stats = await run_cluster_partition(1, client=FakeLLM("no soy json"))
    assert (stats.blobs, stats.groups, stats.rejected) == (1, 0, 0)
    with connection() as c:
        st = c.execute(
            text("SELECT status FROM relation_clusters WHERE id = :c"), {"c": cid}
        ).scalar_one()
    assert st == "candidate"  # queda, se reintenta


@pytest.mark.asyncio
async def test_reparticion_preserva_identidad() -> None:
    # un hijo confirmed existente {P0,P1} + un blob derivado {P0,P1,P2}: el grupo que matchea se
    # SINCRONIZA en sitio (mismo id, mismo nombre), no se crea uno nuevo.
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        child_id = _seed_confirmed_child(c, [p[0], p[1]], name="Original", blob_sig="oldblob")
        _seed_candidate(c, p)
    g = '{"groups":[{"members":[1,2,3],"name":"Renombrado","description":"","confidence":0.9}]}'
    stats = await run_cluster_partition(1, client=FakeLLM(g))
    assert (stats.created, stats.synced) == (0, 1)
    rows = _confirmed_rows()
    assert len(rows) == 1
    assert rows[0]["id"] == child_id  # MISMO id → identidad preservada
    assert rows[0]["name"] == "Original"  # el nombre NO se pisa al crecer
    assert rows[0]["member_count"] == 3  # creció con P2


@pytest.mark.asyncio
async def test_no_toca_aristas_confirmed_deterministas() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        _seed_candidate(c, p)
        det = propose_edge(
            c,
            1,
            p[0],
            p[1],
            producer=PRODUCER_IDENTIDADES,
            relation_type="afiliado",
            status="confirmed",
        )
    stats = await run_cluster_partition(1, client=FakeLLM(_G3))
    assert stats.promoted == 0  # no hay pistas; la confirmed determinista no cuenta
    det_edge = _edges_by_id()[det]
    assert det_edge.status == "confirmed" and det_edge.producer == PRODUCER_IDENTIDADES


# --- rejected_edges (poda a nivel ARISTA, veredicto explícito del LLM) -------------- #


@pytest.mark.asyncio
async def test_rejected_edges_rechaza_la_pista_y_promueve_el_resto() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        _seed_candidate(c, p)
        e01 = _pista(c, p[0], p[1])  # vetada explícitamente por el LLM
        e12 = _pista(c, p[1], p[2])  # intra-grupo → promueve como siempre
    fake = FakeLLM(
        '{"groups":[{"members":[1,2,3],"name":"Ctx","description":"d","confidence":0.9}],'
        '"rejected_edges":["1-2"]}'
    )
    stats = await run_cluster_partition(1, client=fake)
    assert (stats.blobs, stats.created, stats.promoted, stats.rejected_edges) == (1, 1, 1, 1)
    edges = _edges_by_id()
    assert edges[e01].status == "rejected"
    assert edges[e12].status == "confirmed"
    with connection() as c:
        decided = c.execute(
            text("SELECT decided_at FROM relation_edges WHERE id = :e"), {"e": e01}
        ).scalar_one()
    assert decided is not None  # veredicto terminal estampado en la arista misma


@pytest.mark.asyncio
async def test_rejected_edges_indices_basura_no_rompen() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        _seed_candidate(c, p)
        e01 = _pista(c, p[0], p[1])
        e12 = _pista(c, p[1], p[2])
    fake = FakeLLM(
        '{"groups":[{"members":[1,2,3],"name":"Ctx","description":"d","confidence":0.9}],'
        '"rejected_edges":["9-9","x-y","3-3"]}'
    )
    stats = await run_cluster_partition(1, client=fake)
    assert (stats.promoted, stats.rejected_edges) == (2, 0)  # basura ignorada = cascada de siempre
    edges = _edges_by_id()
    assert edges[e01].status == "confirmed" and edges[e12].status == "confirmed"


@pytest.mark.asyncio
async def test_rejected_edges_no_toca_confirmed_determinista() -> None:
    # El LLM "veta" el par 1-2, pero entre P0 y P1 solo hay una afiliación determinista confirmed:
    # el filtro estricto de la cascada (pista+inbox+co-ocurrencia) la protege.
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        _seed_candidate(c, p)
        det = propose_edge(
            c,
            1,
            p[0],
            p[1],
            producer=PRODUCER_IDENTIDADES,
            relation_type="afiliado",
            status="confirmed",
        )
        e12 = _pista(c, p[1], p[2])
    fake = FakeLLM(
        '{"groups":[{"members":[1,2,3],"name":"Ctx","description":"d","confidence":0.9}],'
        '"rejected_edges":["1-2"]}'
    )
    stats = await run_cluster_partition(1, client=fake)
    assert (stats.promoted, stats.rejected_edges) == (1, 0)
    edges = _edges_by_id()
    assert edges[det].status == "confirmed" and edges[det].producer == PRODUCER_IDENTIDADES
    assert edges[e12].status == "confirmed"


@pytest.mark.asyncio
async def test_skip_por_tamano(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cluster_max_members", 2)
    with connection() as c:
        _seed_candidate(c, [_person(c, f"P{i}") for i in range(3)])
    fake = FakeLLM(_G3)
    stats = await run_cluster_partition(1, client=fake)
    assert stats.skipped == 1 and fake.calls == 0


@pytest.mark.asyncio
async def test_quota_propaga() -> None:
    with connection() as c:
        _seed_candidate(c, [_person(c, f"P{i}") for i in range(3)])
    with pytest.raises(LLMQuotaError):
        await run_cluster_partition(1, client=QuotaLLM())
