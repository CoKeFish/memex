"""Slice 6: lazo de sugerencia de intereses (rechazo manual → afinar la lista de intereses).

Cubre la minería sobre marcas manuales (umbral-gated, una llamada LLM falsa), la persistencia de
propuestas y su resolución (aceptar → alta/baja del interés; rechazar → solo estado), más los
endpoints `/relevance/interests/{suggestions,mine}` y `.../resolve`.
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
from memex.relevance.interest_mining import (
    list_suggestions,
    resolve_suggestion,
    run_interest_mining,
)
from memex.relevance.interests import create_interest, list_interests
from memex.relevance.settings import upsert_settings


def _seed_source() -> int:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO sources (user_id, name, type) VALUES (1, 'mail', 'imap') RETURNING id"
            )
        ).scalar()
    assert sid is not None
    return int(sid)


def _seed_mark(sid: int, ext: str, *, is_relevant: bool, minute: int = 0) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id"
            ),
            {
                "sid": sid,
                "eid": ext,
                "occ": datetime(2026, 6, 1, 12, minute, tzinfo=UTC),
                "p": json.dumps({"from": {"email": "promo@steam.com"}, "subject": ext}),
            },
        ).scalar()
        assert iid is not None
        set_mark(c, user_id=1, inbox_id=int(iid), is_relevant=is_relevant)
    return int(iid)


class _FakeLLM:
    """Juez/afinador falso: propone agregar un interés (registra si lo llamaron)."""

    def __init__(self, calls: list[int] | None = None) -> None:
        self.calls = calls if calls is not None else []

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        model: str | None = None,
        response_format: ResponseFormat = "text",
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResult:
        self.calls.append(1)
        body = {"suggestions": [{"action": "add", "text": "descuentos de Steam", "rationale": "x"}]}
        return LLMResult(
            content=json.dumps(body),
            model="fake",
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _enable_gate() -> None:
    with connection() as c:
        upsert_settings(c, 1, enabled=True)


def test_interest_mining_proposes_from_marks() -> None:
    sid = _seed_source()
    for i in range(5):  # umbral default = 5 marcas
        _seed_mark(sid, f"m{i}", is_relevant=True, minute=i)
    _enable_gate()
    stats = asyncio.run(run_interest_mining(1, client=_FakeLLM()))
    assert stats.marks == 5
    assert (stats.proposed, stats.inserted) == (1, 1)
    with connection() as c:
        sugg = list_suggestions(c, user_id=1)
    assert len(sugg) == 1
    assert sugg[0]["action"] == "add"
    assert sugg[0]["text"] == "descuentos de Steam"


def test_interest_mining_below_threshold_skips_llm() -> None:
    sid = _seed_source()
    _seed_mark(sid, "m1", is_relevant=True)  # 1 marca < umbral
    _enable_gate()
    fake = _FakeLLM()
    stats = asyncio.run(run_interest_mining(1, client=fake))
    assert stats.proposed == 0
    assert fake.calls == []  # no se llamó al LLM


def test_interest_mining_disabled_gate_noop() -> None:
    sid = _seed_source()
    for i in range(5):
        _seed_mark(sid, f"m{i}", is_relevant=True, minute=i)
    fake = _FakeLLM()
    stats = asyncio.run(run_interest_mining(1, client=fake))  # gate apagado
    assert (stats.marks, stats.proposed) == (0, 0)
    assert fake.calls == []


def test_resolve_add_creates_interest() -> None:
    with connection() as c:
        sid = c.execute(
            text(
                "INSERT INTO interest_suggestions (user_id, action, text) "
                "VALUES (1, 'add', 'recibos de Uber') RETURNING id"
            )
        ).scalar()
    assert sid is not None
    with connection() as c:
        row = resolve_suggestion(c, user_id=1, suggestion_id=int(sid), accept=True)
    assert row is not None and row["status"] == "accepted"
    with connection() as c:
        assert any(i["text"] == "recibos de Uber" for i in list_interests(c, 1))


def test_resolve_remove_disables_interest() -> None:
    with connection() as c:
        intr = create_interest(c, 1, "todo lo de marketing")
        sid = c.execute(
            text(
                "INSERT INTO interest_suggestions (user_id, action, text, interest_id) "
                "VALUES (1, 'remove', 'todo lo de marketing', :iid) RETURNING id"
            ),
            {"iid": intr["id"]},
        ).scalar()
    assert sid is not None
    with connection() as c:
        resolve_suggestion(c, user_id=1, suggestion_id=int(sid), accept=True)
    with connection() as c:
        enabled = [i["text"] for i in list_interests(c, 1, enabled_only=True)]
    assert "todo lo de marketing" not in enabled  # apagado (reversible), no borrado


def test_interest_suggestion_endpoints(client: Any) -> None:
    # mine con gate apagado → 422
    assert client.post("/relevance/interests/mine").status_code == 422
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO interest_suggestions (user_id, action, text) "
                "VALUES (1, 'add', 'alertas de seguridad')"
            )
        )
    items = client.get("/relevance/interests/suggestions").json()["items"]
    assert len(items) == 1
    sid = items[0]["id"]
    r = client.post(f"/relevance/interests/suggestions/{sid}/resolve", json={"accept": True})
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    # ya resuelta → 404 al re-resolver
    assert (
        client.post(
            f"/relevance/interests/suggestions/{sid}/resolve", json={"accept": True}
        ).status_code
        == 404
    )
