"""Loop del daemon de ingesta: tick, backoff, supervivencia a errores, stop, y reconcile por fuente.

`run_fetch_window` se stubea (este archivo testea la lógica del loop, no la ingesta real) y
`_desired_sources` se stubea para los tests de reconcile (la lectura de DB la cubre
test_api_ingest_scheduler.py). Sin datos reales ni DB acá.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from memex.ingest_scheduler.daemon import IngestScheduler, ScheduledSource
from memex.ingestors.runner import RunStats


def _src(source_id: int, interval_s: float, *, source_type: str = "imap") -> ScheduledSource:
    return ScheduledSource(
        source_id=source_id,
        source_type=source_type,
        cfg={},
        account_id=None,
        interval_s=interval_s,
    )


def _sched(sources: list[ScheduledSource]) -> IngestScheduler:
    return IngestScheduler(user_id=1, sources=sources, tick_seconds=0.01)


def _stub_rfw(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Reemplaza run_fetch_window por un fake async que registra kwargs y devuelve RunStats()."""
    calls: list[dict[str, Any]] = []

    async def fake(**kw: object) -> RunStats:
        calls.append(dict(kw))
        return RunStats()

    monkeypatch.setattr("memex.ingest_scheduler.daemon.run_fetch_window", fake)
    return calls


@pytest.mark.asyncio
async def test_tick_runs_due_source_with_daemon_trigger(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rfw(monkeypatch)
    sched = _sched([_src(10, 3600.0)])
    sched._runtimes[10].next_run_at = 0.0  # forzar vencido
    await sched._tick(time.monotonic())
    assert len(calls) == 1
    assert calls[0]["source_id"] == 10
    assert calls[0]["mode"] == "incremental"
    assert calls[0]["trigger"] == "daemon"
    assert calls[0]["dry_run"] is False


@pytest.mark.asyncio
async def test_tick_skips_not_due_source(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _stub_rfw(monkeypatch)
    # next_run_at = now + interval → no vencido al primer tick.
    sched = _sched([_src(10, 3600.0)])
    await sched._tick(time.monotonic())
    assert calls == []


@pytest.mark.asyncio
async def test_failure_triggers_backoff_and_loop_survives(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(**_kw: object) -> RunStats:
        raise RuntimeError("fetch x")

    monkeypatch.setattr("memex.ingest_scheduler.daemon.run_fetch_window", boom)
    sched = _sched([_src(10, 3600.0)])
    rt = sched._runtimes[10]
    rt.next_run_at = 0.0
    await sched._tick(time.monotonic())  # NO debe propagar
    assert rt.failure_count == 1
    assert rt.next_run_at > time.monotonic()  # reprogramado con backoff


@pytest.mark.asyncio
async def test_success_resets_failures_and_reschedules(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_rfw(monkeypatch)
    sched = _sched([_src(10, 3600.0)])
    rt = sched._runtimes[10]
    rt.next_run_at = 0.0
    rt.failure_count = 3
    await sched._tick(time.monotonic())
    assert rt.failure_count == 0
    assert rt.next_run_at > time.monotonic()


@pytest.mark.asyncio
async def test_stop_event_exits_run_forever() -> None:
    sched = _sched([_src(10, 3600.0)])
    sched.request_stop()  # stop ya seteado → run_forever sale sin tocar la DB
    await asyncio.wait_for(sched.run_forever(), timeout=1.0)


@pytest.mark.asyncio
async def test_no_sources_idles_without_error() -> None:
    sched = _sched([])  # desarmado
    sched.request_stop()
    await asyncio.wait_for(sched.run_forever(), timeout=1.0)


def test_init_seeds_next_run_in_future() -> None:
    sched = IngestScheduler(user_id=7, sources=[_src(10, 3600.0)], tick_seconds=1.0)
    assert set(sched._runtimes) == {10}
    assert sched._runtimes[10].next_run_at > time.monotonic()


def test_reload_preserves_timing_and_refreshes_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = _sched([_src(10, 3600.0)])
    rt = sched._runtimes[10]
    original_next = rt.next_run_at
    rt.failure_count = 2
    # Misma fuente, intervalo + config nuevos.
    desired = [ScheduledSource(10, "imap", {"folder": "X"}, 5, 1800.0)]
    monkeypatch.setattr(sched, "_desired_sources", lambda: desired)
    sched._reload_sources_if_needed()
    rt2 = sched._runtimes[10]
    assert rt2 is rt  # instancia preservada
    assert rt2.next_run_at == original_next  # el intervalo nuevo NO resetea el timing
    assert rt2.failure_count == 2  # preservado
    assert rt2.interval_s == 1800.0  # refrescado en vivo
    assert rt2.cfg == {"folder": "X"}
    assert rt2.account_id == 5


def test_reload_adds_new_and_drops_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = _sched([_src(10, 3600.0)])
    monkeypatch.setattr(sched, "_desired_sources", lambda: [_src(20, 60.0, source_type="telegram")])
    sched._reload_sources_if_needed()
    assert set(sched._runtimes) == {20}  # 10 removida, 20 agregada
    assert sched._runtimes[20].next_run_at > time.monotonic()  # reseed en el futuro


def test_reload_none_preserves_current(monkeypatch: pytest.MonkeyPatch) -> None:
    sched = _sched([_src(10, 3600.0)])
    monkeypatch.setattr(sched, "_desired_sources", lambda: None)  # simula error de DB
    sched._reload_sources_if_needed()
    assert set(sched._runtimes) == {10}  # sin cambios
