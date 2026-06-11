"""Zona gris del resolver (LLMClient FALSO, sin red). Cubre: `parse_verdicts` ultra-defensivo,
confirm grounded → confirmed (quote+confianza al historial), confirm sin cita → dejar + ungrounded,
baja confianza → dejar, todos-reject → rejected, mezcla → dejar, multi-mensaje (un confirm gana),
presupuesto (par a medias queda pendiente real), `LLMQuotaError` aplica lo pagado y propaga, render
con OCR.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text

from memex.config import settings
from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.llm.client import LLMQuotaError
from memex.relations.decisions import add_edge_sources, latest_decisions
from memex.relations.edges import (
    PRODUCER_INBOX,
    RELTYPE_COOCURRENCIA,
    Ref,
    propose_edge,
)
from memex.relations.resolve import ResolveStats, run_resolve
from memex.relations.resolve_llm import load_rendered, parse_verdicts


def _result(content: str) -> LLMResult:
    return LLMResult(
        content=content,
        model="fake",
        usage=LLMUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        cost_usd=Decimal("0"),
        latency_ms=1,
        finish_reason="stop",
    )


class FakeLLM:
    """Devuelve siempre el mismo `content`; cuenta llamadas y guarda los prompts."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls = 0
        self.prompts: list[str] = []

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
        self.prompts.append(messages[-1].content)
        return _result(self.content)


class SeqLLM:
    """Una respuesta (o excepción) por llamada, en orden."""

    def __init__(self, seq: list[str | Exception]) -> None:
        self.seq = seq
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
        item = self.seq[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return _result(item)


# --- siembra ------------------------------------------------------------------------ #


def _exec(sql: str, **p: Any) -> Any:
    with connection() as c:
        r = c.execute(text(sql), p)
        return r.scalar() if r.returns_rows else None


def _source(name: str) -> int:
    return int(
        _exec(
            "INSERT INTO sources (user_id, name, type) VALUES (1, :n, 'imap') RETURNING id", n=name
        )
    )


def _inbox(source_id: int, ext: str, body: str) -> int:
    return int(
        _exec(
            "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
            "VALUES (1, :sid, :ext, NOW(), CAST(:pl AS JSONB)) RETURNING id",
            sid=source_id,
            ext=ext,
            pl=json.dumps({"subject": "Aviso", "body_text": body}),
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


def _edge_status(eid: int) -> str:
    return str(_exec("SELECT status FROM relation_edges WHERE id=:e", e=eid))


def _run(client: Any, **kw: Any) -> ResolveStats:
    return asyncio.run(run_resolve(1, client=client, **kw))


def _verdict(pair: int, verdict: str, quote: str = "", conf: float = 0.9) -> dict[str, Any]:
    return {"pair": pair, "verdict": verdict, "quote": quote, "confidence": conf}


def _resp(*verdicts: dict[str, Any]) -> str:
    return json.dumps({"verdicts": list(verdicts)})


_BODY = "Gracias por tu compra de Celeste en la plataforma; el cargo llega aparte."


# --- parse_verdicts ------------------------------------------------------------------- #


def test_parse_verdicts_defensivo() -> None:
    assert parse_verdicts("no es json", 3) == {}
    assert parse_verdicts("[]", 3) == {}
    assert parse_verdicts('{"verdicts": "x"}', 3) == {}
    out = parse_verdicts(
        json.dumps(
            {
                "verdicts": [
                    {"pair": 1, "verdict": "confirm", "quote": "q", "confidence": 1.7},
                    {"pair": 1, "verdict": "reject"},  # dup: el primero gana
                    {"pair": True, "verdict": "reject"},  # bool-como-int
                    {"pair": 9, "verdict": "reject"},  # fuera de 1..n
                    {"pair": 2, "verdict": "quizas"},  # verdict inválido
                    {"pair": 3, "verdict": "dejar", "quote": 5, "confidence": "x"},
                    "basura",
                ]
            }
        ),
        3,
    )
    assert set(out) == {1, 3}
    assert out[1].verdict == "confirm" and out[1].confidence == 1.0  # clampeada
    assert out[3].quote == "" and out[3].confidence == 0.0


# --- veredictos ------------------------------------------------------------------------ #


def test_confirm_grounded_confirma_con_historial() -> None:
    src = _source("g1")
    m = _inbox(src, "x1", _BODY)
    a, b = _person("Steam"), _person("Celeste")
    eid = _pista(a, b, [m])
    fake = FakeLLM(_resp(_verdict(1, "confirm", "compra de Celeste", 0.9)))
    stats = _run(fake)
    assert fake.calls == 1
    assert stats.llm_confirmed == 1 and stats.ungrounded == 0
    assert _edge_status(eid) == "confirmed"
    with connection() as c:
        dec = latest_decisions(c, 1, [eid])[eid]
    assert dec.verdict == "confirm" and dec.method == "llm"
    assert dec.inbox_id == m and dec.quote == "compra de Celeste"
    assert dec.confidence == Decimal("0.9")


def test_confirm_sin_cita_degrada_a_dejar() -> None:
    src = _source("g2")
    m = _inbox(src, "x2", _BODY)
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [m])
    fake = FakeLLM(_resp(_verdict(1, "confirm", "esto no está en el mensaje", 0.95)))
    stats = _run(fake)
    assert stats.ungrounded == 1 and stats.llm_confirmed == 0 and stats.llm_dejar == 1
    assert _edge_status(eid) == "pista"
    with connection() as c:
        dec = latest_decisions(c, 1, [eid])[eid]
    assert dec.verdict == "dejar" and dec.method == "llm"
    # memo vigente: la próxima corrida lo salta sin LLM
    fake2 = FakeLLM(_resp())
    stats2 = _run(fake2)
    assert stats2.skipped_dejar == 1 and fake2.calls == 0


def test_confianza_baja_no_confirma() -> None:
    src = _source("g3")
    m = _inbox(src, "x3", _BODY)
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [m])
    fake = FakeLLM(_resp(_verdict(1, "confirm", "compra de Celeste", 0.4)))
    stats = _run(fake)
    assert stats.llm_confirmed == 0 and stats.llm_dejar == 1
    assert _edge_status(eid) == "pista"


def test_reject_unanime_rechaza() -> None:
    src = _source("g4")
    m = _inbox(src, "x4", _BODY)
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [m])
    fake = FakeLLM(_resp(_verdict(1, "reject", "", 0.9)))
    stats = _run(fake)
    assert stats.llm_rejected == 1
    assert _edge_status(eid) == "rejected"
    with connection() as c:
        dec = latest_decisions(c, 1, [eid])[eid]
    assert dec.verdict == "reject" and dec.inbox_id == m


def test_mezcla_y_omitido_dejan_memo() -> None:
    src = _source("g5")
    m1 = _inbox(src, "x5a", _BODY)
    m2 = _inbox(src, "x5b", _BODY)
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [m1, m2])
    # m1: reject; m2: el LLM OMITE el par (= no decidió) → mezcla → memo dejar
    seq = SeqLLM([_resp(_verdict(1, "reject", "", 0.9)), _resp()])
    stats = _run(seq)
    assert seq.calls == 2
    assert stats.llm_rejected == 0 and stats.llm_dejar == 1
    assert _edge_status(eid) == "pista"


def test_multimensaje_un_confirm_gana_y_salta_el_resto() -> None:
    src = _source("g6")
    m1 = _inbox(src, "x6a", _BODY)
    m2 = _inbox(src, "x6b", _BODY)
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [m1, m2])
    # m1 confirma con cita → el par sale confirmado; m2 ya no se llama (sin pares pendientes)
    seq = SeqLLM([_resp(_verdict(1, "confirm", "compra de Celeste", 0.9))])
    stats = _run(seq)
    assert seq.calls == 1  # la segunda llamada nunca ocurre
    assert stats.llm_confirmed == 1
    assert _edge_status(eid) == "confirmed"


# --- presupuesto + cuota ----------------------------------------------------------------- #


def test_budget_deja_pendiente_sin_memo() -> None:
    src = _source("g7")
    m1 = _inbox(src, "x7a", _BODY)
    m2 = _inbox(src, "x7b", _BODY)
    a, b = _person("A"), _person("B")
    eid = _pista(a, b, [m1, m2])
    # presupuesto 1: solo m1 se juzga (reject); el par queda A MEDIAS → ni terminal ni memo
    fake = FakeLLM(_resp(_verdict(1, "reject", "", 0.9)))
    stats = _run(fake, max_llm_calls=1)
    assert fake.calls == 1 and stats.budget_exhausted is True
    assert _edge_status(eid) == "pista"
    with connection() as c:
        assert latest_decisions(c, 1, [eid]) == {}  # pendiente real: se reintenta


def test_quota_aplica_lo_pagado_y_propaga() -> None:
    src = _source("g8")
    m1 = _inbox(src, "x8a", _BODY)
    m2 = _inbox(src, "x8b", _BODY)
    a, b = _person("A"), _person("B")
    c2, d2 = _person("C"), _person("D")
    e1 = _pista(a, b, [m1])
    e2 = _pista(c2, d2, [m2])
    seq = SeqLLM(
        [_resp(_verdict(1, "confirm", "compra de Celeste", 0.9)), LLMQuotaError(402, "sin saldo")]
    )
    with pytest.raises(LLMQuotaError):
        _run(seq)
    assert _edge_status(e1) == "confirmed"  # lo pagado se aplicó antes de propagar
    assert _edge_status(e2) == "pista"


def test_error_de_un_mensaje_no_frena() -> None:
    src = _source("g9")
    m1 = _inbox(src, "x9a", _BODY)
    m2 = _inbox(src, "x9b", _BODY)
    a, b = _person("A"), _person("B")
    c2, d2 = _person("C"), _person("D")
    e1 = _pista(a, b, [m1])
    e2 = _pista(c2, d2, [m2])
    seq = SeqLLM([RuntimeError("boom"), _resp(_verdict(1, "confirm", "compra de Celeste", 0.9))])
    stats = _run(seq)
    assert stats.errors == 1 and stats.llm_confirmed == 1
    assert {_edge_status(e1), _edge_status(e2)} == {"pista", "confirmed"}


# --- render ------------------------------------------------------------------------------ #


def test_load_rendered_incluye_ocr_y_trunca() -> None:
    src = _source("g10")
    m = _inbox(src, "x10", "cuerpo corto")
    _exec(
        "INSERT INTO media_assets (user_id, inbox_id, sha256, object_key, bucket, content_type, "
        "size_bytes, ocr_status, ocr_text) "
        "VALUES (1, :m, 'sh', 'k', 'b', 'image/png', 1, 'ok', 'FACTURA N.123 por Celeste')",
        m=m,
    )
    with connection() as c:
        rendered = load_rendered(c, 1, m)
        missing = load_rendered(c, 1, 999_999)
    assert "cuerpo corto" in rendered
    assert "FACTURA N.123 por Celeste" in rendered
    assert len(rendered) <= settings.resolve_render_max_chars
    assert missing == ""
