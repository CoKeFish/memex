"""Validador LLM de cúmulos (LLMClient FALSO, sin red). Cubre: parseo ultra-defensivo
(`parse_verdict`); el worker `run_cluster_validation` (keep confirma+nombra+poda+materializa;
reject memoiza; gate de confianza; poda que deja <2 → reject; JSON basura → candidate sin memo; skip
por tamaño; solo valida pendientes; `LLMQuotaError` propaga)."""

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
from memex.relations.clustering import cluster_signature
from memex.relations.clusters_llm import parse_verdict, run_cluster_validation
from memex.relations.edges import RELTYPE_MIEMBRO_DE, Ref, list_edges


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


def _seed_cluster(conn: Connection, status: str, members: list[Ref]) -> int:
    sig = cluster_signature(members)
    cid = int(
        conn.execute(
            text(
                "INSERT INTO relation_clusters (user_id, status, signature, member_count) "
                "VALUES (1, :st, :sig, :mc) RETURNING id"
            ),
            {"st": status, "sig": sig, "mc": len(members)},
        ).scalar_one()
    )
    for r in members:
        conn.execute(
            text(
                "INSERT INTO relation_cluster_members "
                "(user_id, cluster_id, member_slug, member_id) VALUES (1, :c, :s, :i)"
            ),
            {"c": cid, "s": r.slug, "i": r.id},
        )
    return cid


def _cluster_row(cid: int) -> dict[str, Any]:
    with connection() as c:
        row = (
            c.execute(
                text("SELECT status, name, confidence FROM relation_clusters WHERE id = :c"),
                {"c": cid},
            )
            .mappings()
            .one()
        )
    return dict(row)


# --- parse_verdict (puro) ---------------------------------------------------------- #


def test_parse_keep() -> None:
    v = parse_verdict(
        '{"verdict":"keep","confidence":0.8,"name":"X","description":"d","prune":[1,2]}', 3
    )
    assert (v.verdict, v.confidence, v.name, v.description, v.prune) == (
        "keep",
        0.8,
        "X",
        "d",
        [1, 2],
    )


def test_parse_garbage_is_none() -> None:
    assert parse_verdict("no soy json", 3).verdict is None
    assert parse_verdict('{"sin":"verdict"}', 3).verdict is None
    assert parse_verdict('{"verdict":"maybe"}', 3).verdict is None  # valor inválido


def test_parse_prune_filtra_y_clampa() -> None:
    # 0 y 4 fuera de 1..3; 2 duplicado; true es bool; confianza 2 → clamp 1.0.
    v = parse_verdict('{"verdict":"reject","confidence":2,"prune":[0,4,2,2,true]}', 3)
    assert v.verdict == "reject"
    assert v.confidence == 1.0
    assert v.prune == [2]


# --- worker (LLM falso) ------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_keep_confirma_nombra_poda_materializa() -> None:
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        cid = _seed_cluster(c, "candidate", p)
    fake = FakeLLM(
        '{"verdict":"keep","confidence":0.9,"name":"Proyecto X","description":"d","prune":[3]}'
    )
    stats = await run_cluster_validation(1, client=fake)
    assert (stats.confirmed, stats.rejected, stats.pruned_members) == (1, 0, 1)
    row = _cluster_row(cid)
    assert row["status"] == "confirmed"
    assert row["name"] == "Proyecto X"
    assert float(row["confidence"]) == 0.9
    with connection() as c:
        edges = [e for e in list_edges(c, 1) if e.relation_type == RELTYPE_MIEMBRO_DE]
        purpose = c.execute(text("SELECT purpose FROM llm_calls WHERE user_id = 1")).scalar_one()
    assert {(e.src.slug, e.src.id) for e in edges} == {(p[0].slug, p[0].id), (p[1].slug, p[1].id)}
    assert all(
        (e.dst.slug, e.dst.id) == ("cumulo", cid) for e in edges
    )  # miembro 3 podado: sin arista
    assert purpose == "graph_cluster_validation"


@pytest.mark.asyncio
async def test_reject_memoiza() -> None:
    with connection() as c:
        cid = _seed_cluster(c, "candidate", [_person(c, f"P{i}") for i in range(3)])
    fake = FakeLLM('{"verdict":"reject","confidence":0.2,"name":"","description":"","prune":[]}')
    stats = await run_cluster_validation(1, client=fake)
    assert (stats.confirmed, stats.rejected) == (0, 1)
    assert _cluster_row(cid)["status"] == "rejected"


@pytest.mark.asyncio
async def test_gate_de_confianza_rechaza() -> None:
    with connection() as c:
        cid = _seed_cluster(c, "candidate", [_person(c, f"P{i}") for i in range(3)])
    fake = FakeLLM('{"verdict":"keep","confidence":0.3,"name":"X","description":"d","prune":[]}')
    stats = await run_cluster_validation(1, client=fake)
    assert (stats.confirmed, stats.rejected) == (0, 1)
    assert _cluster_row(cid)["status"] == "rejected"


@pytest.mark.asyncio
async def test_poda_deja_menos_de_dos_rechaza() -> None:
    with connection() as c:
        cid = _seed_cluster(c, "candidate", [_person(c, f"P{i}") for i in range(3)])
    fake = FakeLLM('{"verdict":"keep","confidence":0.9,"name":"X","description":"d","prune":[2,3]}')
    stats = await run_cluster_validation(1, client=fake)
    assert stats.rejected == 1  # sobreviviría 1 < 2 → no se confirma
    assert _cluster_row(cid)["status"] == "rejected"


@pytest.mark.asyncio
async def test_json_basura_queda_candidate_sin_memo() -> None:
    with connection() as c:
        cid = _seed_cluster(c, "candidate", [_person(c, f"P{i}") for i in range(3)])
    fake = FakeLLM("no soy json")
    stats = await run_cluster_validation(1, client=fake)
    assert (stats.confirmed, stats.rejected, stats.clusters) == (0, 0, 1)
    assert _cluster_row(cid)["status"] == "candidate"  # se reintenta la próxima


@pytest.mark.asyncio
async def test_skip_por_tamano(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "cluster_max_members", 2)
    with connection() as c:
        _seed_cluster(c, "candidate", [_person(c, f"P{i}") for i in range(3)])
    fake = FakeLLM('{"verdict":"keep","confidence":0.9,"name":"X","description":"d","prune":[]}')
    stats = await run_cluster_validation(1, client=fake)
    assert stats.skipped == 1
    assert fake.calls == 0


@pytest.mark.asyncio
async def test_solo_valida_pendientes() -> None:
    # un cúmulo ya confirmed (sin needs_revalidation) NO se re-valida → ni llama al LLM.
    with connection() as c:
        _seed_cluster(c, "confirmed", [_person(c, f"P{i}") for i in range(3)])
    fake = FakeLLM('{"verdict":"keep","confidence":0.9,"name":"X","description":"d","prune":[]}')
    stats = await run_cluster_validation(1, client=fake)
    assert (stats.clusters, fake.calls) == (0, 0)


@pytest.mark.asyncio
async def test_quota_propaga() -> None:
    with connection() as c:
        _seed_cluster(c, "candidate", [_person(c, f"P{i}") for i in range(3)])
    with pytest.raises(LLMQuotaError):
        await run_cluster_validation(1, client=QuotaLLM())


@pytest.mark.asyncio
async def test_reject_colision_con_memo_no_revienta() -> None:
    # Un confirmed con needs_revalidation cuya firma coincide con un memo rejected existente, al ser
    # rechazado, se BORRA (no UPDATE a rejected → evitaría el choque del índice único parcial).
    with connection() as c:
        p = [_person(c, f"P{i}") for i in range(3)]
        _seed_cluster(c, "rejected", p)  # memo con esa firma
        cid = _seed_cluster(c, "confirmed", p)  # misma membresía → misma firma
        c.execute(
            text("UPDATE relation_clusters SET needs_revalidation = TRUE WHERE id = :i"), {"i": cid}
        )
    fake = FakeLLM('{"verdict":"reject","confidence":0.2,"name":"","description":"","prune":[]}')
    stats = await run_cluster_validation(1, client=fake)
    assert stats.rejected == 1
    with connection() as c:
        exists = c.execute(
            text("SELECT 1 FROM relation_clusters WHERE id = :i"), {"i": cid}
        ).scalar()
        n_rejected = c.execute(
            text("SELECT count(*) FROM relation_clusters WHERE user_id = 1 AND status = 'rejected'")
        ).scalar_one()
    assert exists is None  # el confirmed se borró (el memo ya registraba el rechazo)
    assert n_rejected == 1  # sin IntegrityError, sin duplicar el memo
