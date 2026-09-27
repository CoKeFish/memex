"""Procesamiento automático de lo nuevo (`procesar_nuevos`): ajustes con defaults, selección por
medio y marca de agua, tope diario y avance de la marca tras procesar."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text

from memex.db import connection
from memex.processing import auto


def _source(stype: str) -> int:
    with connection() as c:
        return int(
            c.execute(
                text("INSERT INTO sources (user_id, name, type) VALUES (1, :n, :t) RETURNING id"),
                {"n": f"{stype}-{uuid4().hex[:6]}", "t": stype},
            ).scalar_one()
        )


def _inbox(source_id: int, received: datetime) -> int:
    with connection() as c:
        return int(
            c.execute(
                text(
                    "INSERT INTO inbox (user_id, source_id, external_id, occurred_at, received_at, "
                    "payload) VALUES (1, :s, :e, :o, :r, CAST(:p AS JSONB)) RETURNING id"
                ),
                {
                    "s": source_id,
                    "e": uuid4().hex,
                    "o": received,
                    "r": received,
                    "p": json.dumps({"text": "hola"}),
                },
            ).scalar_one()
        )


def test_settings_defaults_and_update() -> None:
    with connection() as c:
        st = auto.get_settings(c, 1)
        assert st.enabled is True
        assert st.kinds == ("chat", "notes")
        assert st.watermarks == {} and st.max_per_run == 40
        assert st.daily_cost_cap_usd == Decimal("1.0")
        st = auto.update_settings(c, 1, kinds=["chat", "notes", "documentos"], max_per_run=10)
        assert st.kinds == ("chat", "notes", "documentos") and st.max_per_run == 10
        try:
            auto.update_settings(c, 1, kinds=["fax"])
            raise AssertionError("medio inválido aceptado")
        except ValueError:
            pass
    assert set(auto.types_for_kinds(["chat"])) == {"telegram", "whatsapp"}
    assert "notion" in auto.types_for_kinds(["notes"])


def test_select_new_filters_by_type_watermark_and_limit() -> None:
    tg, mail = _source("telegram"), _source("imap")
    t0 = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    old = _inbox(tg, t0 - timedelta(hours=2))
    a = _inbox(tg, t0 + timedelta(minutes=1))
    b = _inbox(tg, t0 + timedelta(minutes=2))
    c_ = _inbox(tg, t0 + timedelta(minutes=3))
    _inbox(mail, t0 + timedelta(minutes=4))  # correo: fuera por medio
    with connection() as c:
        rows = auto.select_new(c, 1, types=["telegram", "whatsapp"], since=t0, limit=2)
        assert [r[0] for r in rows] == [a, b]
        rows = auto.select_new(c, 1, types=["telegram"], since=t0, limit=10)
        assert [r[0] for r in rows] == [a, b, c_]
        assert old not in [r[0] for r in rows]
        assert auto.select_new(c, 1, types=[], since=t0, limit=10) == []


def test_run_processes_new_and_advances_watermark() -> None:
    tg = _source("telegram")
    now = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    _inbox(tg, now - timedelta(days=3))  # más viejo que el arranque (hace un día): no entra
    m1 = _inbox(tg, now - timedelta(hours=3))
    m2 = _inbox(tg, now - timedelta(hours=1))
    calls: list[dict[str, Any]] = []

    async def fake_reprocess(
        user_id: int, *, stages: list[str], targets: list[int]
    ) -> dict[str, Any]:
        calls.append({"user_id": user_id, "stages": stages, "targets": targets})
        return {
            "cost_usd": 0.0123,
            "results": {"extract": {"ok": len(targets), "cost_usd": 0.0123}},
        }

    stats = asyncio.run(auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now))
    assert stats.targets == 2 and stats.skipped == "" and stats.cost_usd == 0.0123
    assert calls[0]["targets"] == [m1, m2] and calls[0]["stages"][0] == "media"
    assert stats.stages == {"extract": {"ok": 2, "cost_usd": 0.0123}}
    assert stats.by_kind == {"chat": 2}
    with connection() as c:
        st = auto.get_settings(c, 1)
        assert st.watermarks["chat"] == now - timedelta(hours=1)
        assert "notes" in st.watermarks  # el medio sin novedades fija su arranque
        assert st.last_targets == 2 and float(st.last_cost_usd) == 0.0123
    # segunda corrida sin nada nuevo: no llama al pipeline
    stats = asyncio.run(auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now))
    assert stats.skipped == "sin_novedades" and len(calls) == 1
    # llega uno nuevo después de la marca: solo ese
    m3 = _inbox(tg, now + timedelta(minutes=5))
    stats = asyncio.run(
        auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now + timedelta(minutes=10))
    )
    assert stats.targets == 1 and calls[-1]["targets"] == [m3]


def test_adding_a_kind_later_does_not_skip_its_backlog() -> None:
    """El correo entró a `kinds` cuando la marca de los chats ya había avanzado: con una marca
    compartida esos correos no se miraban nunca. Con una por medio, el nuevo arranca de su
    ventana."""
    tg, mail = _source("telegram"), _source("imap")
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    correo_viejo = _inbox(mail, now - timedelta(hours=20))  # antes de que el chat avanzara
    _inbox(tg, now - timedelta(hours=2))
    calls: list[list[int]] = []

    async def fake_reprocess(_uid: int, *, stages: list[str], targets: list[int]) -> dict[str, Any]:
        calls.append(targets)
        return {"cost_usd": 0.0, "results": {}}

    # 1) solo chats: la marca del chat avanza a hace 2 horas
    asyncio.run(auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now))
    with connection() as c:
        st = auto.get_settings(c, 1)
        assert st.watermarks["chat"] == now - timedelta(hours=2)
        # 2) se suma el correo
        auto.update_settings(c, 1, kinds=["chat", "notes", "email"])
    asyncio.run(
        auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now + timedelta(minutes=1))
    )
    assert correo_viejo in calls[-1], "el correo viejo tiene que entrar igual"
    with connection() as c:
        assert auto.get_settings(c, 1).watermarks["email"] == now - timedelta(hours=20)


def test_each_kind_gets_its_share_of_the_run() -> None:
    """Un medio hablador no deja sin turno a los demás: la cuota de la corrida se reparte."""
    tg, mail = _source("telegram"), _source("imap")
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    for i in range(10):
        _inbox(tg, now - timedelta(hours=10, minutes=i))
    correos = [_inbox(mail, now - timedelta(hours=9, minutes=i)) for i in range(3)]
    calls: list[list[int]] = []

    async def fake_reprocess(_uid: int, *, stages: list[str], targets: list[int]) -> dict[str, Any]:
        calls.append(targets)
        return {"cost_usd": 0.0, "results": {}}

    with connection() as c:
        auto.update_settings(c, 1, kinds=["chat", "email"], max_per_run=6)
    stats = asyncio.run(auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now))
    # 6 por corrida entre 2 medios = 3 de cada uno (el correo solo tiene 3)
    assert stats.by_kind == {"chat": 3, "email": 3}
    assert all(c_ in calls[0] for c_ in correos)


def test_daily_cap_and_disabled_skip_without_processing() -> None:
    tg = _source("telegram")
    now = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    _inbox(tg, now - timedelta(hours=1))
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO llm_calls (user_id, purpose, model, prompt_tokens, "
                "completion_tokens, cost_usd, latency_ms, status, created_at) "
                "VALUES (1, 'extract', 'm', 1, 1, 5.0, 1, 'ok', :at)"
            ),
            {"at": now - timedelta(minutes=30)},
        )
        assert auto.cost_today_usd(c, 1, now) >= Decimal("5.0")
    called = False

    async def fake_reprocess(*_a: Any, **_k: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return {}

    stats = asyncio.run(auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now))
    assert stats.skipped == "tope_diario" and not called
    with connection() as c:
        auto.update_settings(c, 1, daily_cost_cap_usd=100, enabled=False)
    stats = asyncio.run(auto.run_auto_processing(1, reprocess_fn=fake_reprocess, now=now))
    assert stats.skipped == "apagado" and not called
    with connection() as c:
        assert auto.get_settings(c, 1).last_skip_reason == "apagado"


def test_api_get_and_patch(client: Any) -> None:
    r = client.get("/processing/auto")
    assert r.status_code == 200 and r.json()["kinds"] == ["chat", "notes"]
    r = client.patch("/processing/auto", json={"max_per_run": 25, "daily_cost_cap_usd": 2.5})
    assert r.status_code == 200
    assert r.json()["max_per_run"] == 25 and r.json()["daily_cost_cap_usd"] == 2.5
    assert client.patch("/processing/auto", json={"kinds": ["fax"]}).status_code == 422
    # la marca se mueve por medio; los demás quedan como están
    r = client.patch(
        "/processing/auto", json={"watermarks": {"email": "2026-09-11T10:00:00+00:00"}}
    )
    assert r.json()["watermarks"]["email"].startswith("2026-09-11T10:00:00")
    r = client.patch("/processing/auto", json={"watermarks": {"chat": "2026-09-12T00:00:00+00:00"}})
    marcas = r.json()["watermarks"]
    assert marcas["email"].startswith("2026-09-11") and marcas["chat"].startswith("2026-09-12")
    r = client.patch("/processing/auto", json={"watermarks": {"email": None}})
    assert "email" not in r.json()["watermarks"]
    assert client.patch("/processing/auto", json={"watermarks": {"fax": None}}).status_code == 422


def test_job_is_registered() -> None:
    from memex.scheduler.jobs import all_jobs

    assert "procesar_nuevos" in all_jobs()
