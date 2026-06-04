"""Pipeline por mensaje: summarize_inbox / extract_inbox + endpoints (con LLM fake, sin red)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from memex.db import connection
from memex.llm import ChatMessage, LLMConfigError, LLMResult, LLMUsage, ResponseFormat


class FakeLLM:
    """LLMClient fake: devuelve un content fijo (texto para resumir, JSON para rutear)."""

    def __init__(self, content: str = "RESUMEN") -> None:
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
            usage=LLMUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            cost_usd=Decimal("0"),
            latency_ms=1,
            finish_reason="stop",
        )


def _seed_classified(source_id: int, eid: str, *, tier: str = "batch", minute: int = 0) -> int:
    with connection() as c:
        iid = c.execute(
            text(
                """
                INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload)
                VALUES (1, :sid, :eid, :occ, CAST(:p AS JSONB)) RETURNING id
                """
            ),
            {
                "sid": source_id,
                "eid": eid,
                "occ": datetime(2026, 5, 31, 10, minute, tzinfo=UTC),
                "p": json.dumps({"subject": eid, "body_text": f"contenido de {eid}"}),
            },
        ).scalar()
        assert iid is not None  # RETURNING id siempre trae la fila recién insertada
        c.execute(
            text(
                "INSERT INTO classifications (user_id, inbox_id, tier, metadata) "
                "VALUES (1, :iid, :tier, '{}'::jsonb)"
            ),
            {"iid": iid, "tier": tier},
        )
    return int(iid)


def test_summarize_individual(client: Any, seed_source: dict[str, Any]) -> None:
    from memex.summarizer.worker import summarize_inbox

    iid = _seed_classified(seed_source["id"], "s1")
    out = asyncio.run(summarize_inbox(1, iid, scope="individual", client=FakeLLM("R1")))
    assert out["status"] == "ok"
    assert out["content"] == "R1"
    detail = client.get(f"/inbox/{iid}").json()
    assert detail["summary"]["content"] == "R1"
    assert detail["summary"]["tier"] == "individual"


def test_summarize_idempotent_then_force(client: Any, seed_source: dict[str, Any]) -> None:
    from memex.summarizer.worker import summarize_inbox

    iid = _seed_classified(seed_source["id"], "s2")
    asyncio.run(summarize_inbox(1, iid, client=FakeLLM("A")))
    again = asyncio.run(summarize_inbox(1, iid, client=FakeLLM("B")))
    assert again["status"] == "already"
    assert again["content"] == "A"  # no re-corre
    forced = asyncio.run(summarize_inbox(1, iid, force=True, client=FakeLLM("C")))
    assert forced["status"] == "ok"
    assert forced["content"] == "C"


def test_summarize_window_groups_neighbors(client: Any, seed_source: dict[str, Any]) -> None:
    from memex.summarizer.worker import summarize_inbox

    sid = seed_source["id"]
    a = _seed_classified(sid, "w1", minute=0)
    b = _seed_classified(sid, "w2", minute=1)
    out = asyncio.run(summarize_inbox(1, a, scope="window", client=FakeLLM("WIN")))
    assert out["status"] == "ok"
    assert out["messages"] == 2  # ambos en la misma ventana
    assert client.get(f"/inbox/{b}").json()["summary"]["content"] == "WIN"


def _enable_modules() -> None:
    with connection() as c:
        for slug in ("finance", "calendar"):
            c.execute(
                text(
                    "INSERT INTO module_settings (user_id, module_slug, enabled) "
                    "VALUES (1, :slug, TRUE) ON CONFLICT (user_id, module_slug) DO NOTHING"
                ),
                {"slug": slug},
            )


def test_extract_individual_routes_nothing(client: Any, seed_source: dict[str, Any]) -> None:
    from memex.modules.orchestrator import extract_inbox

    _enable_modules()
    iid = _seed_classified(seed_source["id"], "e1")
    out = asyncio.run(extract_inbox(1, iid, client=FakeLLM('{"modules": []}')))
    assert out["status"] == "ok"
    assert out["items"] == 0
    assert out["finance"] == []
    assert out["calendar"] == []


def test_force_re_extract_clears_all_modules_via_registry(
    client: Any, seed_source: dict[str, Any]
) -> None:
    """force=True borra las filas de TODOS los módulos registrados por la puerta `forget_inbox`
    (iterando el registry) — incluido identidades, que el borrado hardcodeado viejo NO limpiaba."""
    from memex.modules.orchestrator import extract_inbox

    _enable_modules()  # ≥1 módulo activo para no cortar por 'no_modules'
    iid = _seed_classified(seed_source["id"], "fx1")
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_finance_expenses
                  (user_id, source_inbox_ids, amount, currency, merchant)
                VALUES (1, ARRAY[:iid]::bigint[], 10.0, 'USD', 'OXXO')
                """
            ),
            {"iid": iid},
        )
        c.execute(
            text(
                """
                INSERT INTO mod_identidades_mentions
                  (user_id, source_inbox_ids, mentioned_name, mentioned_kind)
                VALUES (1, ARRAY[:iid]::bigint[], 'Ana', 'persona')
                """
            ),
            {"iid": iid},
        )

    # Routing devuelve [] → no re-extrae; solo nos importa el borrado previo.
    asyncio.run(extract_inbox(1, iid, force=True, client=FakeLLM('{"modules": []}')))

    with connection() as c:
        fin = c.execute(
            text("SELECT count(*) FROM mod_finance_expenses WHERE :iid = ANY(source_inbox_ids)"),
            {"iid": iid},
        ).scalar()
        ide = c.execute(
            text(
                "SELECT count(*) FROM mod_identidades_mentions WHERE :iid = ANY(source_inbox_ids)"
            ),
            {"iid": iid},
        ).scalar()
    assert fin == 0
    assert ide == 0  # identidades AHORA se limpia (antes el borrado hardcodeado lo omitía)


def test_force_re_extract_preserves_shared_rows(client: Any, seed_source: dict[str, Any]) -> None:
    """Una fila COMPARTIDA por varios mensajes (fusionada por dedup) NO se borra al reprocesar uno:
    se le saca solo esa referencia y sobrevive con los mensajes restantes."""
    from memex.modules.orchestrator import extract_inbox

    _enable_modules()
    a = _seed_classified(seed_source["id"], "shA")
    b = _seed_classified(seed_source["id"], "shB")
    with connection() as c:
        c.execute(
            text(
                """
                INSERT INTO mod_finance_expenses
                  (user_id, source_inbox_ids, amount, currency, merchant)
                VALUES (1, ARRAY[:a, :b]::bigint[], 150.0, 'MXN', 'OXXO')
                """
            ),
            {"a": a, "b": b},
        )

    # Reprocesar SOLO el mensaje `a` (routing vacío → no re-extrae): la fila debe quedar con `b`.
    asyncio.run(extract_inbox(1, a, force=True, client=FakeLLM('{"modules": []}')))

    with connection() as c:
        row = c.execute(
            text(
                "SELECT source_inbox_ids FROM mod_finance_expenses "
                "WHERE user_id = 1 AND merchant = 'OXXO'"
            )
        ).first()
    assert row is not None  # la fila compartida sobrevive
    assert list(row[0]) == [b]  # se le sacó `a`, queda `b`


def test_summarize_endpoint_409_when_not_classified(
    client: Any, seed_source: dict[str, Any]
) -> None:
    with connection() as c:
        iid = c.execute(
            text(
                "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, payload) "
                "VALUES (1, :sid, 'nc', '2026-05-31T10:00:00Z', '{}'::jsonb) RETURNING id"
            ),
            {"sid": seed_source["id"]},
        ).scalar()
    assert client.post(f"/inbox/{iid}/summarize").status_code == 409


def test_summarize_endpoint_404(client: Any) -> None:
    assert client.post("/inbox/999999/summarize").status_code == 404


def test_summarize_endpoint_422_without_llm_key(
    client: Any, seed_source: dict[str, Any], monkeypatch: Any
) -> None:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise LLMConfigError("DEEPSEEK_API_KEY not set")

    monkeypatch.setattr("memex.summarizer.worker.LLMConfig.from_env", _boom)
    iid = _seed_classified(seed_source["id"], "nokey")
    r = client.post(f"/inbox/{iid}/summarize")
    assert r.status_code == 422
