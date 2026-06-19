"""Fase de confirmación POR-MENSAJE (`relations.per_message`, metodología B) con LLMClient FALSO.

Cubre: parseo defensivo (`parse_confirm`); a-priori del recibo (confirma sin LLM, extracted);
confirmación LLM con compuerta alias-aware (confirmed/inferred + relation + dirty + resumen);
degradación cuando un extremo no aparece en el cuerpo (gated); dry-run no escribe.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.relations.decisions import add_edge_sources
from memex.relations.edges import (
    PRODUCER_INBOX,
    RELTYPE_COOCURRENCIA,
    Ref,
    get_edge,
    list_edges,
    propose_edge,
)
from memex.relations.per_message import parse_confirm, run_per_message_confirm
from tests.relations._graph_seed import calendar, finance, mention, pair, person


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


# --- helpers de siembra ------------------------------------------------------------ #


def _identity(c: Any, kind: str, name: str) -> int:
    return int(
        c.execute(
            text(
                "INSERT INTO mod_identidades (user_id, kind, display_name) "
                "VALUES (1, :k, :n) RETURNING id"
            ),
            {"k": kind, "n": name},
        ).scalar_one()
    )


def _inbox(c: Any, body: str) -> int:
    sid = c.execute(
        text("INSERT INTO sources (user_id, name, type) VALUES (1, 'corr', 'imap') RETURNING id")
    ).scalar_one()
    payload = json.dumps({"subject": "Pago", "body_text": body})
    return int(
        c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, 'm1', :occ, CAST(:p AS JSONB)) RETURNING id"
            ),
            {"sid": sid, "occ": datetime(2026, 6, 3, 12, 0, tzinfo=UTC), "p": payload},
        ).scalar_one()
    )


def _cooc(c: Any, a: Ref, b: Ref, inbox_id: int) -> int:
    eid = propose_edge(
        c,
        1,
        a,
        b,
        producer=PRODUCER_INBOX,
        relation_type=RELTYPE_COOCURRENCIA,
        evidence=f"inbox:{inbox_id}",
    )
    add_edge_sources(c, eid, [inbox_id])
    return eid


def _recibo(c: Any, inbox_id: int) -> None:
    c.execute(
        text(
            "INSERT INTO mod_finance_transactions "
            "(user_id, source_inbox_ids, direction, amount, currency, occurred_at) "
            "VALUES (1, :ids, 'egreso', 10.00, 'USD', :occ)"
        ),
        {"ids": [inbox_id], "occ": datetime(2026, 6, 3, 12, 0, tzinfo=UTC)},
    )


def _classify(c: Any, inbox_id: int, tier: str) -> None:
    c.execute(
        text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :i, :t)"),
        {"i": inbox_id, "t": tier},
    )


# --- parse_confirm (puro) ---------------------------------------------------------- #


def test_parse_confirm_valido() -> None:
    raw = (
        '{"verdicts": [{"pair": 1, "verdict": "confirm", "relation": "pagó", '
        '"confidence": 0.9}], "summary": "resumen"}'
    )
    verdicts, summary = parse_confirm(raw, 1)
    assert summary == "resumen"
    assert verdicts[1].verdict == "confirm" and verdicts[1].relation == "pagó"
    assert verdicts[1].confidence == 0.9


def test_parse_confirm_confirm_sin_relation_degrada_a_dejar() -> None:
    raw = '{"verdicts": [{"pair": 1, "verdict": "confirm", "relation": "", "confidence": 0.9}]}'
    verdicts, _ = parse_confirm(raw, 1)
    assert verdicts[1].verdict == "dejar"


def test_parse_confirm_descarta_ids_fuera_de_rango_y_basura() -> None:
    assert parse_confirm("no json", 2) == ({}, "")
    verdicts, _ = parse_confirm(
        '{"verdicts": [{"pair": 9, "verdict": "confirm", "relation": "x"}]}', 2
    )
    assert verdicts == {}


# --- a-priori del recibo (sin LLM) ------------------------------------------------- #


@pytest.mark.asyncio
async def test_confirma_por_recibo_sin_llm() -> None:
    with connection() as c:
        p = Ref("identidades:person", _identity(c, "persona", "Juan"))
        o = Ref("identidades:org", _identity(c, "organizacion", "Acme"))
        iid = _inbox(c, "contenido")
        _cooc(c, p, o, iid)
        _recibo(c, iid)
    fake = FakeLLM("{}")
    stats = await run_per_message_confirm(1, client=fake)
    assert stats.confirmed_recibo == 1
    assert fake.calls == 0  # a-priori NO llama al LLM
    with connection() as c:
        e = get_edge(c, 1, p, o, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA)
    assert e is not None and e.verdict == "confirmed" and e.provenance == "extracted"


# --- confirmación LLM con compuerta ------------------------------------------------ #


@pytest.mark.asyncio
async def test_llm_confirma_pasa_compuerta_y_persiste_resumen() -> None:
    with connection() as c:
        p = Ref("identidades:person", _identity(c, "persona", "Juan Niebla"))
        o = Ref("identidades:org", _identity(c, "organizacion", "Acme"))
        iid = _inbox(c, "Juan Niebla le pago a Acme la factura del mes")
        _cooc(c, p, o, iid)
        _classify(c, iid, "individual")  # individual → el juicio persiste su resumen
    fake = FakeLLM(
        '{"verdicts": [{"pair": 1, "verdict": "confirm", "relation": "pagó la factura", '
        '"confidence": 0.95}], "summary": "Juan le pagó a Acme."}'
    )
    stats = await run_per_message_confirm(1, client=fake)
    assert (stats.llm_confirmed, stats.gated, stats.summaries) == (1, 0, 1)
    with connection() as c:
        e = get_edge(c, 1, p, o, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA)
        # dirty marcado en los dos vértices
        n_dirty = c.execute(
            text("SELECT count(*) FROM relation_vertex_state WHERE user_id=1 AND dirty")
        ).scalar()
        # resumen de B persistido y linkeado al inbox
        summ = c.execute(
            text(
                "SELECT s.content, s.tier FROM summaries s "
                "JOIN summary_inbox_links sl ON sl.summary_id = s.id WHERE sl.inbox_id = :i"
            ),
            {"i": iid},
        ).first()
    assert e is not None
    assert e.verdict == "confirmed" and e.provenance == "inferred"
    assert e.relation == "pagó la factura" and e.label == "INFERRED"
    assert n_dirty == 2
    assert summ is not None and summ[0] == "Juan le pagó a Acme." and summ[1] == "individual"


@pytest.mark.asyncio
async def test_compuerta_bloquea_si_un_extremo_no_aparece() -> None:
    # El LLM confirma, pero "Acme" NO está en el cuerpo → la compuerta degrada a ambiguous (gated).
    with connection() as c:
        p = Ref("identidades:person", _identity(c, "persona", "Juan Niebla"))
        o = Ref("identidades:org", _identity(c, "organizacion", "Acme"))
        iid = _inbox(c, "Juan Niebla mando un correo")  # sin 'Acme'
        _cooc(c, p, o, iid)
    fake = FakeLLM(
        '{"verdicts": [{"pair": 1, "verdict": "confirm", "relation": "x", "confidence": 0.95}], '
        '"summary": "s"}'
    )
    stats = await run_per_message_confirm(1, client=fake)
    assert (stats.llm_confirmed, stats.gated) == (0, 1)
    with connection() as c:
        e = get_edge(c, 1, p, o, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA)
    # evaluada completa sin confirm → queda ambigua, pero la IA la miró (inferred)
    assert e is not None and e.verdict == "ambiguous" and e.provenance == "inferred"
    assert e.label == "AMBIGUOUS (inferred)"


@pytest.mark.asyncio
async def test_dry_run_no_escribe() -> None:
    with connection() as c:
        p = Ref("identidades:person", _identity(c, "persona", "Juan"))
        o = Ref("identidades:org", _identity(c, "organizacion", "Acme"))
        iid = _inbox(c, "Juan y Acme")
        _cooc(c, p, o, iid)
    fake = FakeLLM("{}")
    stats = await run_per_message_confirm(1, dry_run=True, client=fake)
    assert stats.edges == 1 and stats.estimated_calls == 1
    assert fake.calls == 0
    with connection() as c:
        e = get_edge(c, 1, p, o, producer=PRODUCER_INBOX, relation_type=RELTYPE_COOCURRENCIA)
    assert e is not None and e.verdict == "ambiguous"  # nada cambió


# --- FASE 3: resumen de las unidades sin resumir (run_summaries) ------------------- #


def _summary_of(inbox_id: int) -> tuple[str, str, dict[str, Any]] | None:
    """(content, tier, metadata) del resumen ligado al mensaje, o None."""
    with connection() as c:
        row = c.execute(
            text(
                "SELECT s.content, s.tier, s.metadata FROM summaries s "
                "JOIN summary_inbox_links sl ON sl.summary_id = s.id WHERE sl.inbox_id = :i"
            ),
            {"i": inbox_id},
        ).first()
    return (row[0], row[1], row[2]) if row is not None else None


@pytest.mark.asyncio
async def test_individual_sin_pares_lo_resume_run_summaries() -> None:
    """Un individual SIN co-ocurrencias (el juicio no lo toca) igual recibe resumen, vía la pasada
    de resumen (tier individual, origin 'summarize')."""
    with connection() as c:
        iid = _inbox(c, "Recordatorio: tu factura vence el viernes")
        _classify(c, iid, "individual")
    stats = await run_per_message_confirm(1, client=FakeLLM("Resumen del correo."))
    assert stats.summaries == 1
    summ = _summary_of(iid)
    assert summ is not None
    assert summ[0] == "Resumen del correo." and summ[1] == "individual"
    assert summ[2]["origin"] == "summarize"


@pytest.mark.asyncio
async def test_chat_se_resume_por_lote() -> None:
    """Chat: nunca individual; su LOTE (varios mensajes contiguos de la misma fuente) se resume en
    UNA sola llamada, ligada a todos sus mensajes (tier batch)."""
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) "
                "VALUES (1, 'tg', 'telegram') RETURNING id"
            )
        ).scalar_one()
        ids: list[int] = []
        for k in range(3):
            iid = c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                    "VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id"
                ),
                {
                    "sid": sid,
                    "eid": f"c{k}",
                    "occ": datetime(2026, 6, 3, 12, k, tzinfo=UTC),
                    "p": json.dumps({"text": f"mensaje {k}"}),
                },
            ).scalar_one()
            _classify(c, int(iid), "batch")
            ids.append(int(iid))
    stats = await run_per_message_confirm(1, client=FakeLLM("Resumen de la conversación."))
    assert stats.summaries == 1  # UN resumen para el lote de 3
    with connection() as c:
        n_links = c.execute(
            text("SELECT count(*) FROM summary_inbox_links WHERE inbox_id = ANY(:ids)"),
            {"ids": ids},
        ).scalar()
    assert n_links == 3  # los 3 ligados al MISMO resumen
    summ = _summary_of(ids[0])
    assert summ is not None and summ[1] == "batch"


@pytest.mark.asyncio
async def test_batch_con_pares_lo_resume_run_summaries_no_el_juicio() -> None:
    """Un mensaje BATCH con pares: el juicio lo confirma pero NO persiste su resumen (sería partir
    el lote); el resumen lo produce run_summaries por LOTE (tier batch, origin 'summarize')."""
    with connection() as c:
        p = Ref("identidades:person", _identity(c, "persona", "Juan Niebla"))
        o = Ref("identidades:org", _identity(c, "organizacion", "Acme"))
        iid = _inbox(c, "Juan Niebla le pago a Acme")
        _cooc(c, p, o, iid)
        _classify(c, iid, "batch")
    fake = FakeLLM(
        '{"verdicts": [{"pair": 1, "verdict": "confirm", "relation": "pagó", '
        '"confidence": 0.95}], "summary": "ignorado en batch"}'
    )
    stats = await run_per_message_confirm(1, client=fake)
    assert stats.llm_confirmed == 1  # el juicio corrió y confirmó la arista
    assert stats.summaries == 1  # 1 resumen, de run_summaries (no del juicio)
    summ = _summary_of(iid)
    assert summ is not None
    assert summ[1] == "batch"  # lote, no individual
    assert summ[2]["origin"] == "summarize"  # run_summaries, no el juicio (graph_confirm)


# --- FASE 2b: PROPUESTA en correos densos (all-type; reemplazo del relevo) --------- #


@pytest.mark.asyncio
async def test_propuesta_densa_emite_aristas_all_type() -> None:
    """Un correo DENSO (más vértices que el cap) lo saltea `generate_cooccurrence` (sin pistas que
    juzgar); la fase de PROPUESTA le pide al LLM los pares relacionados ALL-TYPE y emite aristas
    confirmed/`producer='llm'` que pasen el grounder. Acá un par calendar↔finance (tipos MIXTOS,
    lo que el relevo solo-identidad jamás haría) se emite; un par con cita ausente se descarta."""
    body = "La reunión del evento se pagó con Uber para la cena; gracias a todos por venir."
    with connection() as c:
        iid = _inbox(c, body)
    fin = finance("Uber", [iid])
    cal = calendar("Reunión del evento", [iid])
    for k in range(7):  # calendar + finance + 7 personas = 9 vértices de contenido > cap (8)
        mention(person(f"P{k}"), [iid])
    # Orden canónico por (slug, id): 1=calendar, 2=finance, 3..9=personas.
    fake = FakeLLM(
        '{"pairs": ['
        '{"a": 1, "b": 2, "relation": "pago del evento", "quote": "se pagó con Uber"}, '
        '{"a": 2, "b": 3, "relation": "involucra", "quote": "cita que no figura en el cuerpo"}'
        "]}"
    )
    stats = await run_per_message_confirm(1, client=fake)
    assert (stats.dense_messages, stats.proposed_edges, stats.proposed_ungrounded) == (1, 1, 1)
    with connection() as c:
        llm_edges = list_edges(c, 1, producer="llm")
    assert len(llm_edges) == 1
    e = llm_edges[0]
    assert e.verdict == "confirmed" and e.provenance == "inferred"
    assert e.relation_type == "co-ocurrencia" and e.relation == "pago del evento"
    assert pair(e) == {("calendar", cal), ("finance", fin)}  # tipos MIXTOS conectados
