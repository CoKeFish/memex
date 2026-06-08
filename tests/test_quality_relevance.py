"""Sistema de calidad: relevancia por remitente.

Cubre (1) la agregación SQL — señal núcleo (hecho no-identidad con `item_count>0` → relevante;
solo-identidad o `item_count=0` → no relevante), el bucket `summarized_only` (se resumió sin hecho),
el `%`/volumen/orden y la `tier_mix`; (2) que el orquestador atribuye el `item_count` POR MENSAJE
(sin sobre-atribuir el total de la ventana en batch); y (3) el endpoint GET /quality/senders.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.core.relevance_marks import set_mark
from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.orchestrator import run_extraction
from memex.quality.relevance import senders_by_relevance

_MESSAGES_MARKER = "Mensajes (JSON):\n"


# --- helpers de seeding (SQL directo) -------------------------------------------- #


def _seed_msg(
    source_id: int, ext: str, *, email: str, tier: str | None, minute: int = 0, body: str = ""
) -> int:
    payload: dict[str, Any] = {"from": {"email": email}, "subject": ext}
    if body:
        payload["body_text"] = body
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id"
            ),
            {
                "sid": source_id,
                "eid": ext,
                "occ": datetime(2026, 5, 28, 12, minute, tzinfo=UTC),
                "p": json.dumps(payload),
            },
        ).scalar()
        if tier is not None:
            c.execute(
                text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :i, :t)"),
                {"i": iid, "t": tier},
            )
    assert iid is not None
    return int(iid)


def _extraction(inbox_id: int, slug: str, count: int) -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_extractions (user_id, module_slug, inbox_id, item_count) "
                "VALUES (1, :s, :i, :c)"
            ),
            {"s": slug, "i": inbox_id, "c": count},
        )


def _summarize(inbox_id: int) -> None:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO summaries (user_id, tier, content) "
                "VALUES (1, 'batch', 'x') RETURNING id"
            )
        ).scalar()
        c.execute(
            text("INSERT INTO summary_inbox_links (summary_id, inbox_id) VALUES (:s, :i)"),
            {"s": sid, "i": inbox_id},
        )


def _item_count(inbox_id: int, slug: str) -> int | None:
    with connection() as c:
        return c.execute(
            text(
                "SELECT item_count FROM module_extractions WHERE inbox_id = :i AND module_slug = :s"
            ),
            {"i": inbox_id, "s": slug},
        ).scalar()


# --- (1) agregación SQL ---------------------------------------------------------- #


def test_relevance_signal_and_buckets(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    # a@x.com: 3 mensajes — con hecho, solo-identidad (inerte) y resumido-sin-hecho.
    a1 = _seed_msg(sid, "a1", email="a@x.com", tier="blacklist", minute=0)
    _extraction(a1, "finance", 1)  # hecho no-identidad → relevante
    a2 = _seed_msg(sid, "a2", email="a@x.com", tier="batch", minute=1)
    _extraction(a2, "identidades", 1)  # SOLO identidad → no relevante, no resumido → inerte
    a3 = _seed_msg(sid, "a3", email="a@x.com", tier="batch", minute=2)
    _extraction(a3, "finance", 0)  # ruteado-fuera/empty (item_count=0) → no relevante
    _summarize(a3)  # pero se resumió → summarized_only
    # b@x.com: 1 mensaje con hecho.
    b1 = _seed_msg(sid, "b1", email="b@x.com", tier="individual", minute=3)
    _extraction(b1, "finance", 2)

    with connection() as c:
        rows = senders_by_relevance(c, user_id=1)
    by_key = {r["sender_key"]: r for r in rows}

    a = by_key["a@x.com"]
    assert (a["messages"], a["relevant"], a["summarized_only"], a["inert"]) == (3, 1, 1, 1)
    assert float(a["relevance_pct"]) == 33.3
    assert a["tier_mix"] == {"blacklist": 1, "batch": 2, "individual": 0, "unclassified": 0}
    assert float(a["volume_ratio"]) == 1.5  # 3 / media(2)

    b = by_key["b@x.com"]
    assert (b["messages"], b["relevant"], b["summarized_only"], b["inert"]) == (1, 1, 0, 0)
    assert float(b["relevance_pct"]) == 100.0

    # Orden: ruido (inerte) primero → a@x.com antes que b@x.com.
    assert rows[0]["sender_key"] == "a@x.com"


def test_chat_without_from_email_groups_by_sender(seed_source: dict[str, Any]) -> None:
    # Telegram: sin `from.email`; agrupa por `sender.user_id` y etiqueta por display_name.
    sid = seed_source["id"]
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, 'tg1', :occ, CAST(:p AS JSONB)) RETURNING id"
            ),
            {
                "sid": sid,
                "occ": datetime(2026, 5, 28, 12, 0, tzinfo=UTC),
                "p": json.dumps(
                    {"chat_id": -100, "sender": {"user_id": 42, "display_name": "Ana"}}
                ),
            },
        ).scalar()
    assert iid is not None
    with connection() as c:
        rows = senders_by_relevance(c, user_id=1)
    assert len(rows) == 1
    assert rows[0]["sender_key"] == "tg:user:42"
    assert rows[0]["sender_label"] == "Ana"


# --- (2) atribución del item_count por el orquestador ---------------------------- #


class _OneFactLLM:
    """LLM falso: produce UN gasto de finanzas atribuido SOLO al primer mensaje del lote (los demás
    quedan en 0 hechos). Verifica que el cursor guarda el conteo POR MENSAJE, no el total de la
    ventana."""

    def __init__(self) -> None:
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
        msgs: list[dict[str, Any]] = json.loads(messages[-1].content.split(_MESSAGES_MARKER, 1)[1])
        first = msgs[0]
        item = {
            "source_inbox_ids": [first["id"]],
            "amount": "10.00",
            "currency": "ARS",
            "counterparty": "Test",
            "occurred_on": None,
            "description": "gasto de prueba",
            "evidence": first["text"],
        }
        return LLMResult(
            content=json.dumps({"items": [item]}),
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def test_orchestrator_attributes_item_count_per_message(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]  # imap → email; finance consume email
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, enabled) "
                "VALUES (1, 'finance', TRUE) ON CONFLICT (user_id, module_slug) DO NOTHING"
            )
        )
    # Lote batch de 2 mensajes; el fake solo atribuye un gasto al PRIMERO.
    m1 = _seed_msg(sid, "m1", email="a@x.com", tier="batch", minute=0, body="pagué $10")
    m2 = _seed_msg(
        sid, "m2", email="a@x.com", tier="batch", minute=1, body="hola, nada que extraer"
    )

    fake = _OneFactLLM()
    stats = asyncio.run(run_extraction(1, client=fake))

    assert stats.items == 1  # un solo hecho en toda la ventana
    # El conteo es POR MENSAJE: m1 produjo el hecho (1), m2 no (0) — NO se sobre-atribuye el total.
    assert _item_count(m1, "finance") == 1
    assert _item_count(m2, "finance") == 0


# --- (3) endpoint ---------------------------------------------------------------- #


def test_quality_senders_endpoint(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    iid = _seed_msg(sid, "e1", email="c@x.com", tier="batch", minute=0)
    _extraction(iid, "finance", 1)

    r = client.get("/quality/senders")
    assert r.status_code == 200
    items = r.json()["items"]
    row = next(it for it in items if it["sender_key"] == "c@x.com")
    assert row["messages"] == 1
    assert row["relevant"] == 1
    assert float(row["relevance_pct"]) == 100.0


# --- (4) override manual (marca por-mensaje) ------------------------------------- #


def test_manual_mark_overrides_heuristic(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    a1 = _seed_msg(sid, "a1", email="a@x.com", tier="batch", minute=0)
    _extraction(a1, "finance", 1)  # heurística: relevante
    a2 = _seed_msg(sid, "a2", email="a@x.com", tier="batch", minute=1)
    _extraction(a2, "identidades", 1)  # heurística: inerte
    with connection() as c:
        set_mark(c, user_id=1, inbox_id=a1, is_relevant=False)  # baja a no-relevante
        set_mark(c, user_id=1, inbox_id=a2, is_relevant=True, reason="sí importaba")  # sube
    with connection() as c:
        rows = senders_by_relevance(c, user_id=1)
    a = next(r for r in rows if r["sender_key"] == "a@x.com")
    # a1 baja de relevante a inerte; a2 sube de inerte a relevante → se cruzan; 2 marcados.
    assert (a["messages"], a["relevant"], a["inert"], a["marked"]) == (2, 1, 1, 2)


def test_relevance_mark_endpoints(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    iid = _seed_msg(sid, "m1", email="c@x.com", tier="batch")

    r = client.post(f"/inbox/{iid}/relevance", json={"is_relevant": False, "reason": "ruido"})
    assert r.status_code == 200
    assert r.json()["is_relevant"] is False

    got = client.get(f"/inbox/{iid}").json()["relevance"]
    assert got["is_relevant"] is False
    assert got["reason"] == "ruido"

    # upsert: re-marcar reemplaza.
    r2 = client.post(f"/inbox/{iid}/relevance", json={"is_relevant": True})
    assert r2.json()["is_relevant"] is True

    # borrar → vuelve a None; borrar de nuevo = 404.
    assert client.delete(f"/inbox/{iid}/relevance").status_code == 204
    assert client.get(f"/inbox/{iid}").json()["relevance"] is None
    assert client.delete(f"/inbox/{iid}/relevance").status_code == 404


def test_relevance_mark_unknown_inbox_is_404(client: Any) -> None:
    assert client.post("/inbox/999999/relevance", json={"is_relevant": False}).status_code == 404
