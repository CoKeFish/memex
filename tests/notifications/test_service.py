"""El ciclo completo: producir → cola → entregar por canal, con idempotencia, severidad, silencio,
lectura previa en el dashboard, LLM inyectado y fallas de canal."""

from __future__ import annotations

from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from memex.db import connection
from memex.modules.tareas.module import add as add_tarea
from memex.notifications import store
from memex.notifications.channels import ChannelError
from memex.notifications.client import Notification
from memex.notifications.producers import Draft
from memex.notifications.service import in_quiet_hours, passes_severity, run_cycle
from memex.notifications.settings import NotificationsEnv, upsert_producer_settings

TZ = ZoneInfo("America/Bogota")
TODAY = datetime.now(TZ).date()  # los avisos vencen al final del día: la fecha debe ser HOY
NOW = datetime.combine(TODAY, time(9, 0), tzinfo=TZ)
ENV = NotificationsEnv(
    _env_file=None,
    user_id=1,
    telegram_bot_token="",
    dashboard_url="https://memex.local",
)


class FakeChannel:
    name = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str, Notification]] = []
        self.fail = fail
        self.closed = False

    async def send(self, target: str, text: str, notification: Notification) -> str | None:
        if self.fail:
            raise ChannelError("api caída")
        self.sent.append((target, text, notification))
        return f"m{len(self.sent)}"

    async def aclose(self) -> None:
        self.closed = True


class FakeWriter:
    def __init__(self, text: str | None = "Redactado por el modelo.") -> None:
        self.text = text
        self.calls = 0

    async def write(self, draft: Draft) -> str | None:
        self.calls += 1
        return self.text

    async def aclose(self) -> None:
        return None


def _channel(**kw: Any) -> int:
    params: dict[str, Any] = {
        "user_id": 1,
        "channel": "fake",
        "target": "7",
        "subscriptions": {"personal": "info", "sistema": "critica"},
    }
    params.update(kw)
    with connection() as c:
        cid = store.upsert_channel(c, **params)
        # El ciclo corre con un `now` simulado (2026-09-10): el canal debe existir desde antes.
        c.execute(
            text("UPDATE notification_channels SET created_at = '2026-01-01' WHERE id = :id"),
            {"id": cid},
        )
        return cid


def _tarea(title: str = "Entregar informe", priority: str = "normal") -> None:
    with connection() as c:
        add_tarea(c, 1, title=title, due_on=TODAY, priority=priority)


def _only(producer: str, **kw: Any) -> dict[str, Any]:
    return {"only_producer": producer, **kw}


@pytest.mark.asyncio
async def test_cycle_produces_enqueues_delivers_once() -> None:
    _channel()
    _tarea()
    ch = FakeChannel()
    stats = await run_cycle(1, env=ENV, now=NOW, channels={"fake": ch}, **_only("tareas_del_dia"))
    assert stats.producers == ["tareas_del_dia"]
    assert (stats.drafts, stats.enqueued, stats.delivered, stats.failed) == (1, 1, 1, 0)
    assert len(ch.sent) == 1
    target, text_, n = ch.sent[0]
    assert target == "7"
    assert text_.startswith("Vence hoy: Entregar informe\n")
    assert text_.endswith("https://memex.local/tareas")  # deep_link con la base configurada
    assert n.audience == "personal" and n.kind == "tareas.vence_hoy"
    # Segundo ciclo: nada nuevo, nada repetido.
    again = await run_cycle(1, env=ENV, now=NOW, channels={"fake": ch}, **_only("tareas_del_dia"))
    assert (again.drafts, again.skipped_existing, again.enqueued, again.delivered) == (1, 1, 0, 0)
    assert len(ch.sent) == 1
    with connection() as c:
        d = store.list_deliveries(c, user_id=1)
        assert len(d) == 1 and d[0]["status"] == "enviada" and d[0]["external_ref"] == "m1"
        assert store.count_unread(c, user_id=1) == 1  # entregar no marca leído en el dashboard
    assert ch.closed is False  # el canal inyectado lo cierra quien lo inyectó


@pytest.mark.asyncio
async def test_read_in_dashboard_is_not_pushed() -> None:
    _channel()
    _tarea()
    # Sin canal inyectado intenta construir «fake», que no existe → error de canal, no entrega.
    stats = await run_cycle(1, env=ENV, now=NOW, **_only("tareas_del_dia"))
    assert stats.enqueued == 1 and stats.delivered == 0
    assert any(e.startswith("canal fake") for e in stats.errors)
    with connection() as c:
        nid = store.list_active(c, user_id=1, limit=5)[0]["id"]
        store.mark_read(c, notification_id=nid, user_id=1)
    ch = FakeChannel()
    stats = await run_cycle(1, env=ENV, now=NOW, channels={"fake": ch}, **_only("tareas_del_dia"))
    assert stats.pending == 0 and ch.sent == []


@pytest.mark.asyncio
async def test_severity_filter_and_quiet_hours() -> None:
    cid = _channel(subscriptions={"personal": "alta"})
    _tarea("Normal")  # info → filtrada
    _tarea("Urgente", priority="alta")  # alta → pasa
    ch = FakeChannel()
    stats = await run_cycle(1, env=ENV, now=NOW, channels={"fake": ch}, **_only("tareas_del_dia"))
    assert stats.enqueued == 2 and stats.delivered == 1 and stats.below_severity == 1
    assert ch.sent[0][1].startswith("Vence hoy: Urgente")
    # Silencio 00:00-23:59 → lo no crítico queda pendiente (no se registra entrega).
    with connection() as c:
        store.update_channel(
            c,
            user_id=1,
            channel_id=cid,
            quiet=(time(0, 0), time(23, 59)),
            subscriptions={"personal": "info"},
        )
    stats = await run_cycle(1, env=ENV, now=NOW, channels={"fake": ch}, **_only("tareas_del_dia"))
    assert stats.held_quiet == 1 and stats.delivered == 0 and len(ch.sent) == 1


def test_quiet_hours_and_severity_helpers() -> None:
    assert in_quiet_hours(time(23, 0), time(22, 0), time(7, 0)) is True  # cruza medianoche
    assert in_quiet_hours(time(6, 59), time(22, 0), time(7, 0)) is True
    assert in_quiet_hours(time(12, 0), time(22, 0), time(7, 0)) is False
    assert in_quiet_hours(time(12, 0), time(9, 0), time(13, 0)) is True
    assert in_quiet_hours(time(12, 0), None, time(13, 0)) is False
    assert passes_severity("info", "personal", {"personal": "info"}) is True
    assert passes_severity("info", "personal", {"personal": "alta"}) is False
    assert passes_severity("critica", "sistema", {"sistema": "critica"}) is True
    assert passes_severity("alta", "sistema", {}) is True  # sin mínimo declarado = info


@pytest.mark.asyncio
async def test_llm_render_used_once_and_template_fallback() -> None:
    _channel()
    writer = FakeWriter()
    stats = await run_cycle(
        1,
        env=ENV,
        now=NOW,
        channels={"fake": FakeChannel()},
        writer=writer,
        **_only("resumen_diario"),
    )
    assert stats.enqueued == 1 and stats.rendered_llm == 1 and writer.calls == 1
    with connection() as c:
        row = store.list_active(c, user_id=1, limit=5)[0]
    assert row["body"] == "Redactado por el modelo." and row["kind"] == "resumen.diario"
    assert row["payload"]["producer"] == "resumen_diario"
    # Ya existe → no se vuelve a redactar (no se paga dos veces).
    await run_cycle(
        1,
        env=ENV,
        now=NOW,
        channels={"fake": FakeChannel()},
        writer=writer,
        **_only("resumen_diario"),
    )
    assert writer.calls == 1
    # Modelo que no devuelve nada → plantilla.
    with connection() as c:
        c.execute(text("DELETE FROM notifications"))
    empty = FakeWriter(text=None)
    stats = await run_cycle(
        1,
        env=ENV,
        now=NOW,
        channels={"fake": FakeChannel()},
        writer=empty,
        **_only("resumen_diario"),
    )
    assert stats.rendered_llm == 0 and empty.calls == 1
    with connection() as c:
        assert store.list_active(c, user_id=1, limit=5)[0]["body"].startswith("Nada pendiente")


@pytest.mark.asyncio
async def test_render_plantilla_setting_skips_llm() -> None:
    with connection() as c:
        upsert_producer_settings(
            c,
            1,
            "resumen_diario",
            default_enabled=True,
            default_render="llm",
            default_config={"hora": "07:00", "dias_tareas": 7, "dias_asuntos": 7},
            render="plantilla",
        )
    writer = FakeWriter()
    stats = await run_cycle(1, env=ENV, now=NOW, writer=writer, **_only("resumen_diario"))
    assert stats.enqueued == 1 and writer.calls == 0


@pytest.mark.asyncio
async def test_disabled_producer_and_dry_run_write_nothing() -> None:
    _channel()
    _tarea()
    with connection() as c:
        upsert_producer_settings(
            c,
            1,
            "tareas_del_dia",
            default_enabled=True,
            default_render="plantilla",
            default_config={"hora": "08:00"},
            enabled=False,
        )
    stats = await run_cycle(
        1, env=ENV, now=NOW, channels={"fake": FakeChannel()}, **_only("tareas_del_dia")
    )
    assert stats.producers == [] and stats.enqueued == 0
    with connection() as c:
        upsert_producer_settings(
            c,
            1,
            "tareas_del_dia",
            default_enabled=True,
            default_render="plantilla",
            default_config={"hora": "08:00"},
            enabled=True,
        )
    ch = FakeChannel()
    stats = await run_cycle(
        1, env=ENV, now=NOW, channels={"fake": ch}, dry_run=True, **_only("tareas_del_dia")
    )
    assert stats.drafts == 1 and stats.enqueued == 0 and ch.sent == []
    assert stats.preview[0]["title"] == "Vence hoy: Entregar informe"
    with connection() as c:
        assert store.list_active(c, user_id=1, limit=5) == []


@pytest.mark.asyncio
async def test_channel_failure_records_fallida_and_retries_later() -> None:
    _channel()
    _tarea()
    bad = FakeChannel(fail=True)
    stats = await run_cycle(1, env=ENV, now=NOW, channels={"fake": bad}, **_only("tareas_del_dia"))
    assert stats.delivered == 0 and stats.failed == 1
    with connection() as c:
        d = store.list_deliveries(c, user_id=1)
        assert (
            d[0]["status"] == "fallida" and d[0]["attempts"] == 1 and "api caída" in d[0]["error"]
        )
    # Dentro de la ventana de reintento no insiste.
    stats = await run_cycle(1, env=ENV, now=NOW, channels={"fake": bad}, **_only("tareas_del_dia"))
    assert stats.pending == 0
    # Pasada la ventana, reintenta y ahora sale.
    good = FakeChannel()
    env = ENV.model_copy(update={"retry_after_s": -1})
    stats = await run_cycle(1, env=env, now=NOW, channels={"fake": good}, **_only("tareas_del_dia"))
    assert stats.delivered == 1 and len(good.sent) == 1
    with connection() as c:
        d = store.list_deliveries(c, user_id=1)
        assert d[0]["status"] == "enviada" and d[0]["attempts"] == 2


@pytest.mark.asyncio
async def test_system_alert_goes_to_channel_only_if_severe_enough() -> None:
    _channel(subscriptions={"personal": "info", "sistema": "critica"})
    with connection() as c:
        c.execute(
            text(
                "INSERT INTO worker_runs (user_id, job, status, started_at, finished_at, error) "
                "VALUES (1, 'extract', 'error', :s, :f, 'timeout')"
            ),
            {"s": datetime(2026, 9, 10, 5, tzinfo=TZ), "f": datetime(2026, 9, 10, 5, 1, tzinfo=TZ)},
        )
    ch = FakeChannel()
    stats = await run_cycle(1, env=ENV, now=NOW, channels={"fake": ch}, **_only("sistema"))
    assert stats.enqueued == 1 and stats.below_severity == 1 and ch.sent == []  # alta < critica
    with connection() as c:
        row = store.list_active(c, user_id=1, limit=5)[0]
    assert row["audience"] == "sistema" and row["kind"] == "sistema.run-failed"
