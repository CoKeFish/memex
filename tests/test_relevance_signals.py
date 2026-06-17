"""Capa de SEÑALES del sistema de relevancia: relevancia por remitente + candidatos.

Cubre (1) la agregación SQL — señal núcleo (hecho no-identidad con `item_count>0` → relevante;
solo-identidad o `item_count=0` → no relevante), el bucket `summarized_only` (se resumió sin hecho),
el `%`/volumen/orden y la `tier_mix`; (2) que el orquestador atribuye el `item_count` POR MENSAJE
(sin sobre-atribuir el total de la ventana en batch); y (3) los procedimientos/candidatos
(`fact_count`, re-evaluación por el motor único) que alimentan `GET /relevance/senders`.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.classifier.worker import run_classification
from memex.core.relevance_marks import set_mark
from memex.core.sender_tiers import list_overrides, set_override
from memex.db import connection
from memex.llm import ChatMessage, LLMResult, LLMUsage, ResponseFormat
from memex.modules.orchestrator import run_extraction
from memex.relevance.candidates import (
    list_candidates,
    reevaluate_candidate,
    run_relevance_detection,
    set_candidate_status,
)
from memex.relevance.procedures import run_candidate_detection
from memex.relevance.settings import upsert_settings
from memex.relevance.signals import senders_by_relevance

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


def _enable_finance() -> None:
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO module_settings (user_id, module_slug, enabled) "
                "VALUES (1, 'finance', TRUE) ON CONFLICT (user_id, module_slug) DO NOTHING"
            )
        )


def _seed_empty_msg(source_id: int, ext: str, *, minute: int = 0) -> int:
    """Mensaje clasificado batch SIN contenido renderizable (payload {}) → camino empty_input."""
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, :eid, :occ, CAST('{}' AS JSONB)) RETURNING id"
            ),
            {"sid": source_id, "eid": ext, "occ": datetime(2026, 5, 28, 12, minute, tzinfo=UTC)},
        ).scalar()
        c.execute(
            text("INSERT INTO classifications (user_id, inbox_id, tier) VALUES (1, :i, 'batch')"),
            {"i": iid},
        )
    assert iid is not None
    return int(iid)


def _seed_tx(inbox_id: int) -> None:
    """Transacción de dominio pre-existente atribuida al mensaje (lo que deja una corrida previa
    cuyo hecho quedó compartido/unido por el dedup)."""
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO mod_finance_transactions "
                "(user_id, source_inbox_ids, direction, amount, currency, occurred_at, "
                " occurred_at_precision, counterparty) "
                "VALUES (1, CAST(:ids AS BIGINT[]), 'egreso', 10, 'USD', :at, 'datetime', 'Tigo')"
            ),
            {"ids": [inbox_id], "at": datetime(2026, 5, 28, 11, 0, tzinfo=UTC)},
        )


def _llm_call(cost: str, *, inbox_id: int | None = None) -> int:
    """Una fila `llm_calls` con `cost_usd` (lote/batch → `inbox_id=None`)."""
    with connection() as c:
        cid = c.execute(
            text(
                "INSERT INTO llm_calls "
                "(user_id, inbox_id, purpose, model, prompt_tokens, completion_tokens, "
                " cost_usd, latency_ms, status) "
                "VALUES (1, :inb, 'extract_grouped', 'fake', 1, 1, "
                "CAST(:cost AS NUMERIC), 1, 'ok') RETURNING id"
            ),
            {"inb": inbox_id, "cost": cost},
        ).scalar()
    assert cid is not None
    return int(cid)


def _llm_node(call_id: int, *, inbox_id: int | None) -> None:
    """Nodo `llm` de `trace_nodes` ligado a una `llm_call` (ancla cost/N al inbox)."""
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO trace_nodes (user_id, inbox_id, kind, llm_call_id) "
                "VALUES (1, :inb, 'llm', :cid)"
            ),
            {"inb": inbox_id, "cid": call_id},
        )


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
    assert rows[0]["kind"] == "chat"


# --- (2) atribución del item_count por el orquestador ---------------------------- #


class _OneFactLLM:
    """LLM falso: produce UN gasto de finanzas atribuido SOLO al primer mensaje del lote (los demás
    quedan en 0 hechos) — o a TODOS con `attribute_all=True` (un hecho compartido). Verifica que el
    cursor guarda el conteo POR MENSAJE atribuido, no el total de la ventana ni lo insertado."""

    def __init__(self, *, attribute_all: bool = False) -> None:
        self.calls = 0
        self._attribute_all = attribute_all

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
            "source_inbox_ids": ([m["id"] for m in msgs] if self._attribute_all else [first["id"]]),
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
    _enable_finance()
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


def test_item_count_atribuido_cuenta_hecho_compartido(seed_source: dict[str, Any]) -> None:
    """Un hecho respaldado por VARIOS mensajes (el dedup/extracción los une en una sola fila)
    cuenta para cada uno: item_count = hechos atribuidos, no "ítems nuevos insertados"."""
    sid = seed_source["id"]
    _enable_finance()
    m1 = _seed_msg(sid, "m1", email="a@x.com", tier="batch", minute=0, body="pagué $10")
    m2 = _seed_msg(sid, "m2", email="a@x.com", tier="batch", minute=1, body="comprobante del pago")

    stats = asyncio.run(run_extraction(1, client=_OneFactLLM(attribute_all=True)))

    assert stats.items == 1  # una sola fila de dominio…
    assert _item_count(m1, "finance") == 1  # …atribuida a ambos mensajes
    assert _item_count(m2, "finance") == 1


def test_force_reprocess_no_degrada_item_count_ni_relevancia(seed_source: dict[str, Any]) -> None:
    """Reprocesar con `force` (borra cursor + desatribuye + re-extrae el mismo hecho) deja el
    item_count y la señal de relevancia como estaban."""
    sid = seed_source["id"]
    _enable_finance()
    m1 = _seed_msg(sid, "m1", email="a@x.com", tier="batch", minute=0, body="pagué $10")
    asyncio.run(run_extraction(1, client=_OneFactLLM()))
    assert _item_count(m1, "finance") == 1

    asyncio.run(run_extraction(1, inbox_ids=[m1], force=True, client=_OneFactLLM()))

    assert _item_count(m1, "finance") == 1
    with connection() as c:
        rows = senders_by_relevance(c, user_id=1)
    a = next(r for r in rows if r["sender_key"] == "a@x.com")
    assert a["relevant"] == 1


def test_empty_input_cursor_cuenta_atribucion_previa(seed_source: dict[str, Any]) -> None:
    """Camino empty_input: el cursor cuenta lo que el dominio YA atribuye al mensaje (antes
    quedaba en 0 y la relevancia lo leía como irrelevante)."""
    sid = seed_source["id"]
    _enable_finance()
    m1 = _seed_empty_msg(sid, "vacio")
    _seed_tx(m1)

    fake = _OneFactLLM()
    asyncio.run(run_extraction(1, client=fake))

    assert fake.calls == 0  # 1 módulo → ruteo short-circuit; lote sin texto → sin LLM
    assert _item_count(m1, "finance") == 1


def test_write_path_escribe_item_count_atribuido(seed_source: dict[str, Any]) -> None:
    """El write-path del orquestador escribe `item_count` = atribución por mensaje
    (`attributed_counts`): un mensaje vacío con un hecho sembrado y un mensaje con cuerpo quedan
    ambos en 1, sea cual sea el camino."""
    sid = seed_source["id"]
    _enable_finance()
    m1 = _seed_empty_msg(sid, "vacio")
    _seed_tx(m1)
    asyncio.run(run_extraction(1, client=_OneFactLLM()))
    m2 = _seed_msg(sid, "m2", email="a@x.com", tier="batch", minute=1, body="pagué $10")
    asyncio.run(run_extraction(1, client=_OneFactLLM()))

    assert _item_count(m1, "finance") == 1
    assert _item_count(m2, "finance") == 1


# --- (3) endpoint ---------------------------------------------------------------- #


def test_quality_senders_endpoint(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    iid = _seed_msg(sid, "e1", email="c@x.com", tier="batch", minute=0)
    _extraction(iid, "finance", 1)

    r = client.get("/relevance/senders")
    assert r.status_code == 200
    items = r.json()["items"]
    row = next(it for it in items if it["sender_key"] == "c@x.com")
    assert row["messages"] == 1
    assert row["relevant"] == 1
    assert row["kind"] == "email"
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


# --- (5) dial de costo: sender→tier (batch/individual; «no procesar» = regla del gate) --- #


def _sender_row(client: Any, key: str) -> dict[str, Any]:
    items = client.get("/relevance/senders").json()["items"]
    return next(s for s in items if s["sender_key"] == key)


def test_sender_tier_override_applies_in_classification(seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    with connection() as c:
        set_override(c, user_id=1, sender_email="a@x.com", tier="individual")
    a = _seed_msg(sid, "a1", email="a@x.com", tier=None)  # sin clasificar aún
    b = _seed_msg(sid, "b1", email="b@x.com", tier=None)
    run_classification(1)
    with connection() as c:
        result = c.execute(
            text("SELECT inbox_id, tier FROM classifications WHERE inbox_id = ANY(:ids)"),
            {"ids": [a, b]},
        ).all()
    tiers: dict[int, str] = {int(iid): str(tier) for iid, tier in result}
    assert tiers[a] == "individual"  # el dial de costo del remitente gana
    assert tiers[b] == "batch"  # heurística normal (sin marcadores de bulk)


def test_sender_tier_endpoints(client: Any, seed_source: dict[str, Any]) -> None:
    sid = seed_source["id"]
    _seed_msg(sid, "m1", email="c@x.com", tier="batch")

    r = client.post(
        "/relevance/senders/tier", json={"sender_email": "c@x.com", "tier": "individual"}
    )
    assert r.status_code == 200
    assert r.json()["tier"] == "individual"

    row = _sender_row(client, "c@x.com")
    assert row["email"] == "c@x.com"
    assert row["override_tier"] == "individual"

    # «blacklist» ya no es un tier válido (no procesar = regla del gate) → 422.
    bad = client.post(
        "/relevance/senders/tier", json={"sender_email": "c@x.com", "tier": "blacklist"}
    )
    assert bad.status_code == 422

    assert client.delete("/relevance/senders/tier?sender_email=c@x.com").status_code == 204
    assert _sender_row(client, "c@x.com")["override_tier"] is None
    assert client.delete("/relevance/senders/tier?sender_email=c@x.com").status_code == 404


def test_sender_tier_list_endpoint(client: Any) -> None:
    assert client.get("/relevance/senders/tiers").json()["items"] == []

    client.post("/relevance/senders/tier", json={"sender_email": "b@x.com", "tier": "individual"})
    client.post(
        "/relevance/senders/tier",
        json={"sender_email": "c@x.com", "tier": "batch", "reason": "agrupar barato"},
    )
    items = client.get("/relevance/senders/tiers").json()["items"]
    by_email = {i["sender_email"]: i for i in items}
    assert set(by_email) == {"b@x.com", "c@x.com"}
    assert by_email["b@x.com"]["tier"] == "individual"
    assert by_email["b@x.com"]["reason"] is None
    assert by_email["c@x.com"]["tier"] == "batch"
    assert by_email["c@x.com"]["reason"] == "agrupar barato"
    assert all(i["created_at"] and i["updated_at"] for i in items)

    # Upsert: re-POST no duplica y reordena (updated_at DESC → el recién tocado primero).
    client.post("/relevance/senders/tier", json={"sender_email": "b@x.com", "tier": "batch"})
    items = client.get("/relevance/senders/tiers").json()["items"]
    assert len(items) == 2
    assert items[0]["sender_email"] == "b@x.com"
    assert items[0]["tier"] == "batch"

    assert client.delete("/relevance/senders/tier?sender_email=c@x.com").status_code == 204
    items = client.get("/relevance/senders/tiers").json()["items"]
    assert [i["sender_email"] for i in items] == ["b@x.com"]


def test_sender_tier_list_scoped_to_owner(client: Any) -> None:
    with connection() as c:
        c.execute(text("INSERT INTO users (id, email, display_name) VALUES (2, 'u2@local', 'u2')"))
        set_override(c, user_id=1, sender_email="mine@x.com", tier="individual")
        set_override(c, user_id=2, sender_email="theirs@x.com", tier="individual")
    items = client.get("/relevance/senders/tiers").json()["items"]  # client = user 1
    assert [i["sender_email"] for i in items] == ["mine@x.com"]
    with connection() as c:
        assert [r["sender_email"] for r in list_overrides(c, user_id=2)] == ["theirs@x.com"]


# --- (6) cola de candidatos (detección automática "por métricas") ---------------- #


def _noisy_sender(seed_source: dict[str, Any], email: str, n: int, *, with_fact: int = 0) -> None:
    """Seed `n` mensajes de `email`; los primeros `with_fact` producen un hecho (relevantes)."""
    sid = seed_source["id"]
    for i in range(n):
        iid = _seed_msg(sid, f"{email}-{i}", email=email, tier="batch", minute=i)
        _extraction(iid, "finance", 1 if i < with_fact else 0)


def test_detect_candidates_flags_noisy_email_senders(seed_source: dict[str, Any]) -> None:
    _noisy_sender(seed_source, "spam@x.com", 6, with_fact=0)  # 0% relevancia → candidato
    _noisy_sender(seed_source, "boss@x.com", 6, with_fact=6)  # 100% → NO
    _noisy_sender(seed_source, "rare@x.com", 2, with_fact=0)  # bajo volumen → NO
    stats = run_candidate_detection(1)  # procedimiento fact_count; defaults min 5, max 10%
    with connection() as c:
        keys = {r["sender_key"] for r in list_candidates(c, user_id=1)}
    assert stats.candidates == 1
    assert "spam@x.com" in keys
    assert "boss@x.com" not in keys
    assert "rare@x.com" not in keys


def test_detect_preserves_dismissed_and_skips_overridden(seed_source: dict[str, Any]) -> None:
    _noisy_sender(seed_source, "spam@x.com", 6, with_fact=0)
    _noisy_sender(seed_source, "bulk@x.com", 6, with_fact=0)
    with connection() as c:
        set_override(c, user_id=1, sender_email="spam@x.com", tier="individual")  # tiene override
    run_candidate_detection(1)
    with connection() as c:
        keys = {r["sender_key"] for r in list_candidates(c, user_id=1, status=None)}
    assert "spam@x.com" not in keys  # con override de tier → no es candidato
    assert "bulk@x.com" in keys
    with connection() as c:
        set_candidate_status(c, user_id=1, sender_key="bulk@x.com", status="dismissed")
    run_candidate_detection(1)  # re-detecta
    with connection() as c:
        status = {r["sender_key"]: r["status"] for r in list_candidates(c, user_id=1, status=None)}
    assert status["bulk@x.com"] == "dismissed"  # no re-abre


def test_candidates_endpoints(client: Any, seed_source: dict[str, Any]) -> None:
    _noisy_sender(seed_source, "spam@x.com", 6, with_fact=0)
    run_relevance_detection(1)  # defaults de settings (min 5, max 10%)
    items = client.get("/relevance/candidates").json()["items"]
    assert any(i["sender_key"] == "spam@x.com" for i in items)

    r = client.post(
        "/relevance/candidates/status", json={"sender_key": "spam@x.com", "status": "dismissed"}
    )
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed"

    open_items = client.get("/relevance/candidates?status=open").json()["items"]
    assert all(i["sender_key"] != "spam@x.com" for i in open_items)
    bad = client.post("/relevance/candidates/status", json={"sender_key": "nope", "status": "open"})
    assert bad.status_code == 404


# --- (7) motor único: re-evaluar un candidato por el juez del gate --------------- #


class _GateLLM:
    """LLM falso del gate: marca relevant cada correo de la ventana (parsea los ids del prompt)."""

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        msgs = json.loads(messages[-1].content.split("Mensajes (JSON):\n", 1)[1])
        verdicts = [{"id": m["id"], "verdict": "relevant", "reason": "test"} for m in msgs]
        return LLMResult(
            content=json.dumps({"verdicts": verdicts}),
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def test_reevaluate_candidate_runs_the_gate_engine(seed_source: dict[str, Any]) -> None:
    """La re-evaluación pasa por el MOTOR ÚNICO (el gate), no por un segundo juez: corre el gate
    sobre la muestra del candidato y persiste veredictos en `relevance_verdicts`."""
    _noisy_sender(seed_source, "spam@x.com", 6, with_fact=0)
    run_candidate_detection(1)  # crea el candidato con su muestra (sample_inbox_ids)
    with connection() as c:
        upsert_settings(c, 1, enabled=True)  # el motor (gate) tiene que estar encendido
    result = asyncio.run(reevaluate_candidate(1, sender_key="spam@x.com", client=_GateLLM()))
    assert result is not None
    assert result["relevant"] >= 1  # el motor juzgó la muestra
    with connection() as c:
        n = c.execute(
            text(
                "SELECT count(*) FROM relevance_verdicts v JOIN inbox i ON i.id = v.inbox_id "
                "WHERE lower(i.payload->'from'->>'email') = 'spam@x.com' AND v.verdict = 'relevant'"
            )
        ).scalar()
    assert n is not None and int(n) >= 1


def test_reevaluate_unknown_candidate_is_none() -> None:
    assert asyncio.run(reevaluate_candidate(1, sender_key="nope@x.com")) is None


# --- (8) atribución de costo LLM por remitente ----------------------------------- #


def test_cost_attribution_per_sender(seed_source: dict[str, Any]) -> None:
    """`cost_usd` por remitente combina: individual con nodo (costo completo), lote
    compartido (cost/N por mensaje) y `llm_call` huérfana sin nodo trace (costo completo).
    Sin actividad LLM → 0."""
    sid = seed_source["id"]
    # (a) individual: 1 call con nodo trace que la referencia (N=1 → costo completo).
    indiv = _seed_msg(sid, "indiv", email="indiv@x.com", tier="individual", minute=0)
    call_a = _llm_call("0.10", inbox_id=indiv)
    _llm_node(call_a, inbox_id=indiv)
    # (b) lote: 1 call batch (inbox NULL) compartida por 2 mensajes del mismo remitente (N=2 →
    #     cost/2 a cada uno → 0.20 al remitente).
    lote1 = _seed_msg(sid, "lote1", email="lote@x.com", tier="batch", minute=1)
    lote2 = _seed_msg(sid, "lote2", email="lote@x.com", tier="batch", minute=2)
    call_b = _llm_call("0.20", inbox_id=None)
    _llm_node(call_b, inbox_id=lote1)
    _llm_node(call_b, inbox_id=lote2)
    # (c) huérfana: call con inbox_id seteado y SIN nodo trace → costo completo vía cost_orphans.
    orphan = _seed_msg(sid, "orphan", email="orphan@x.com", tier="batch", minute=3)
    _llm_call("0.05", inbox_id=orphan)
    # (d) sin actividad LLM → 0.
    _seed_msg(sid, "free", email="free@x.com", tier="batch", minute=4)

    with connection() as c:
        rows = senders_by_relevance(c, user_id=1)
    cost = {r["sender_key"]: float(r["cost_usd"]) for r in rows}

    assert cost["indiv@x.com"] == 0.10
    assert cost["lote@x.com"] == 0.20
    assert cost["orphan@x.com"] == 0.05
    assert cost["free@x.com"] == 0.0


def test_cost_shared_call_with_null_inbox_node_not_overattributed(
    seed_source: dict[str, Any],
) -> None:
    """N cuenta TODOS los nodos de la call (incl. el desempate async con inbox NULL): una call de
    0.30 referenciada por nodos de a, b y un nodo inbox-NULL → N=3; a y b reciben 0.10 (no 0.15) y
    la porción del nodo NULL no se atribuye a ningún remitente."""
    sid = seed_source["id"]
    a = _seed_msg(sid, "a", email="a@x.com", tier="batch", minute=0)
    b = _seed_msg(sid, "b", email="b@x.com", tier="batch", minute=1)
    call = _llm_call("0.30", inbox_id=None)
    _llm_node(call, inbox_id=a)
    _llm_node(call, inbox_id=b)
    _llm_node(call, inbox_id=None)  # desempate async: cuenta en N pero su porción no se atribuye

    with connection() as c:
        rows = senders_by_relevance(c, user_id=1)
    cost = {r["sender_key"]: float(r["cost_usd"]) for r in rows}
    assert cost["a@x.com"] == 0.10
    assert cost["b@x.com"] == 0.10
    # La porción del nodo inbox-NULL (0.10) no aparece en ningún remitente.
    assert round(sum(cost.values()), 6) == 0.20
